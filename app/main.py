"""
app/main.py

FastAPI Orchestrator Hub — Dynamic Ops Automation Engine
Entry point for the unified API layer.

Responsibilities:
  1. Application lifespan: initialize and tear down ConfigManager, DB pools, etc.
  2. Global middleware stack: request ID injection, tenant resolution, CORS,
     structured logging, and timing headers.
  3. Unified /health endpoint returning deep component status.
  4. Router mounting for all sub-domain API modules.
  5. Global exception handlers for structured error envelope responses.

Architectural constraints:
  - Every request that touches tenant-scoped resources MUST carry an
    X-Tenant-ID header (enforced by TenantMiddleware).
  - No business logic lives in this file; it is pure orchestration wiring.
  - All error responses conform to ErrorEnvelope (Pydantic v2 model).
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.config_manager import (
    ConfigManager,
    TenantInactiveError,
    TenantNotFoundError,
    get_app_settings,
    get_config_manager,
    reset_config_manager,
)
from app.core.models.tenant import TenantContext

# ---------------------------------------------------------------------------
# Logging bootstrap
# Structured JSON logging is configured here so it is active before the first
# request arrives. Production log aggregators (Datadog, Loki, CloudWatch) parse
# this format natively.
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "msg": %(message)s}',
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class ErrorEnvelope(BaseModel):
    """Canonical error response shape returned by all exception handlers."""

    model_config = ConfigDict(frozen=True)

    error_code: str = Field(description="Machine-readable error code slug.")
    message: str = Field(description="Human-readable error message.")
    request_id: str | None = Field(
        default=None,
        description="Correlation ID for tracing this request across logs.",
    )
    details: list[dict[str, Any]] | None = Field(
        default=None,
        description="Structured validation error details (present for 422 responses).",
    )


class ComponentHealth(BaseModel):
    """Health status of a single infrastructure component."""

    model_config = ConfigDict(frozen=True)

    name: str
    status: str  # "ok" | "degraded" | "down"
    latency_ms: float | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    """Aggregated health response from the /health endpoint."""

    model_config = ConfigDict(frozen=True)

    status: str  # "ok" | "degraded" | "down"
    version: str
    environment: str
    uptime_seconds: float
    request_id: str
    components: list[ComponentHealth]


# ---------------------------------------------------------------------------
# Application startup time (used for uptime calculation)
# ---------------------------------------------------------------------------

_APP_START_TIME: float = time.monotonic()


# ---------------------------------------------------------------------------
# Lifespan context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Manages the full startup → request serving → shutdown lifecycle.

    Startup:
      - Load AppSettings (validates all env vars at boot, fails fast).
      - Initialize ConfigManager (warms tenant cache, validates registry).
      - Store references in app.state for middleware and health checks.

    Shutdown:
      - Flush any pending metrics/traces.
      - Gracefully drain the tenant cache and close registry connections.
    """
    settings = get_app_settings()

    # Configure log level from settings now that settings are loaded.
    logging.getLogger().setLevel(settings.log_level)

    logger.info(
        '"Starting %s v%s [env=%s]"',
        settings.app_name,
        settings.version,
        settings.environment,
    )

    # Initialize ConfigManager singleton.
    config_manager = await get_config_manager()
    app.state.config_manager = config_manager
    app.state.settings = settings

    logger.info('"Application startup complete."')

    yield  # ← Application serves requests here.

    logger.info('"Application shutdown initiated."')
    await reset_config_manager()
    logger.info('"Application shutdown complete."')


# ---------------------------------------------------------------------------
# FastAPI application factory
# ---------------------------------------------------------------------------


def create_application() -> FastAPI:
    """
    Factory function for the FastAPI application instance.
    Using a factory (rather than module-level instantiation) enables clean
    test isolation — each test can call create_application() for a fresh instance.
    """
    settings = get_app_settings()

    app = FastAPI(
        title="Dynamic Ops Automation Engine",
        description=(
            "Multi-tenant async operations orchestrator. "
            "All endpoints require X-Tenant-ID header unless documented otherwise."
        ),
        version=settings.version,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
        root_path=settings.api_root_path,
    )

    # ----------------------------------------------------------------
    # Middleware stack (applied in reverse registration order by Starlette)
    # ----------------------------------------------------------------

    # 1. CORS — must be outermost to handle preflight OPTIONS requests.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time-Ms", "X-Tenant-ID"],
    )

    # 2. Request ID injection — stamps every request with a UUID correlation ID.
    app.add_middleware(RequestIDMiddleware)

    # 3. Timing header — adds X-Response-Time-Ms to every response.
    app.add_middleware(TimingMiddleware)

    # 4. Tenant resolution — resolves X-Tenant-ID to TenantContext and stores
    #    in request.state.tenant. Rejects unknown/inactive tenants with 403/404.
    #    EXEMPT paths bypass tenant resolution entirely.
    app.add_middleware(
        TenantMiddleware,
        exempt_paths={"/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico"},
    )

    # ----------------------------------------------------------------
    # Exception handlers
    # ----------------------------------------------------------------

    app.add_exception_handler(RequestValidationError, _validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(TenantNotFoundError, _tenant_not_found_handler)  # type: ignore[arg-type]
    app.add_exception_handler(TenantInactiveError, _tenant_inactive_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_exception_handler)  # type: ignore[arg-type]

    # ----------------------------------------------------------------
    # ----------------------------------------------------------------
    # Routers
    # Mount sub-domain routers here as they are delivered in subsequent phases.
    # ----------------------------------------------------------------
    from .api.v1.routers.ingestion import router as ingestion_router
    from .api.v1.routers.events import router as events_router

    app.include_router(ingestion_router)
    app.include_router(events_router)
    # ----------------------------------------------------------------

    # ----------------------------------------------------------------
    # Core endpoints (non-tenant-scoped)
    # ----------------------------------------------------------------

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["Platform"],
        summary="Deep health check for all infrastructure components.",
        status_code=status.HTTP_200_OK,
    )
    async def health_check(request: Request) -> HealthResponse:
        """
        Returns the operational status of the engine and its dependencies.

        - status="ok": All components healthy.
        - status="degraded": One or more non-critical components impaired.
        - status="down": A critical component is unavailable.

        This endpoint is exempt from tenant resolution middleware and is
        safe to call without X-Tenant-ID.
        """
        request_id: str = getattr(request.state, "request_id", str(uuid.uuid4()))
        components: list[ComponentHealth] = []
        overall_status = "ok"

        # --- ConfigManager / Tenant registry health ---
        cm: ConfigManager | None = getattr(request.app.state, "config_manager", None)
        if cm is not None:
            registry_healthy = await cm._registry.health_check()  # noqa: SLF001
            cache_stats = cm.cache_stats()
            components.append(
                ComponentHealth(
                    name="tenant_registry",
                    status="ok" if registry_healthy else "down",
                    detail=(
                        f"backend={cache_stats['backend']}, "
                        f"cache_size={cache_stats['size']}/{cache_stats['max_size']}"
                    ),
                )
            )
            if not registry_healthy:
                overall_status = "down"
        else:
            components.append(
                ComponentHealth(
                    name="tenant_registry",
                    status="down",
                    detail="ConfigManager not initialized.",
                )
            )
            overall_status = "down"

        # --- Database connectivity probe (future: replace with real pool ping) ---
        # When the DB pool is wired in Phase 4, swap this block with an actual
        # SELECT 1 probe against the async pool:
        #   t0 = time.monotonic()
        #   await db_pool.fetchval("SELECT 1")
        #   latency_ms = (time.monotonic() - t0) * 1000
        components.append(
            ComponentHealth(
                name="database",
                status="ok",
                detail="Pool not yet wired (Phase 4).",
            )
        )

        # --- Redis connectivity probe (future: replace with PING) ---
        components.append(
            ComponentHealth(
                name="redis",
                status="ok",
                detail="Pool not yet wired (Phase 4).",
            )
        )

        app_settings: Any = getattr(request.app.state, "settings", get_app_settings())
        http_status = status.HTTP_200_OK if overall_status == "ok" else status.HTTP_503_SERVICE_UNAVAILABLE

        response_data = HealthResponse(
            status=overall_status,
            version=app_settings.version,
            environment=app_settings.environment,
            uptime_seconds=round(time.monotonic() - _APP_START_TIME, 3),
            request_id=request_id,
            components=components,
        )

        # We need to return the correct HTTP status code for degraded/down states
        # while still using JSONResponse for status overriding.
        if http_status != status.HTTP_200_OK:
            return JSONResponse(  # type: ignore[return-value]
                status_code=http_status,
                content=response_data.model_dump(),
            )
        return response_data

    return app


# ---------------------------------------------------------------------------
# Middleware implementations
# ---------------------------------------------------------------------------


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Stamps every inbound request with a UUID v4 correlation ID.

    - Reads X-Request-ID header if provided by an upstream proxy/gateway.
    - Generates a fresh UUID if absent.
    - Stores in request.state.request_id for use by all downstream handlers.
    - Echoes the ID in the X-Request-ID response header.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


class TimingMiddleware(BaseHTTPMiddleware):
    """
    Measures total request wall-clock time and emits it in X-Response-Time-Ms.
    Used by API consumers and load balancers to detect latency regressions.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - start) * 1_000, 3)
        response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
        return response


class TenantMiddleware(BaseHTTPMiddleware):
    """
    Universal tenant resolution middleware.

    For every non-exempt path:
      1. Reads the X-Tenant-ID request header.
      2. Resolves it to a TenantContext via ConfigManager (cache-first).
      3. Stores the TenantContext in request.state.tenant.
      4. Validates the tenant is active.

    Failure modes:
      - Missing X-Tenant-ID header     → 400 Bad Request
      - Unknown tenant_id              → 404 Not Found
      - Inactive tenant                → 403 Forbidden
      - ConfigManager not initialized  → 503 Service Unavailable
    """

    def __init__(self, app: Any, exempt_paths: set[str]) -> None:
        super().__init__(app)
        self._exempt_paths = exempt_paths

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Strip query string for path matching.
        path = request.url.path.rstrip("/") or "/"
        if path in self._exempt_paths:
            return await call_next(request)

        tenant_id = request.headers.get("X-Tenant-ID", "").strip()
        if not tenant_id:
            return _json_error(
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code="missing_tenant_id",
                message="X-Tenant-ID header is required for this endpoint.",
                request_id=getattr(request.state, "request_id", None),
            )

        cm: ConfigManager | None = getattr(request.app.state, "config_manager", None)
        if cm is None:
            logger.error('"ConfigManager not available in app.state during request."')
            return _json_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                error_code="service_unavailable",
                message="Configuration service is not available. Retry momentarily.",
                request_id=getattr(request.state, "request_id", None),
            )

        try:
            tenant_context: TenantContext = await cm.get_tenant(tenant_id)
        except TenantNotFoundError:
            return _json_error(
                status_code=status.HTTP_404_NOT_FOUND,
                error_code="tenant_not_found",
                message=f"Tenant '{tenant_id}' does not exist.",
                request_id=getattr(request.state, "request_id", None),
            )
        except TenantInactiveError:
            return _json_error(
                status_code=status.HTTP_403_FORBIDDEN,
                error_code="tenant_inactive",
                message=f"Tenant '{tenant_id}' is currently inactive.",
                request_id=getattr(request.state, "request_id", None),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception('"Unexpected error resolving tenant: %s"', exc)
            return _json_error(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                error_code="tenant_resolution_error",
                message="An internal error occurred while resolving tenant configuration.",
                request_id=getattr(request.state, "request_id", None),
            )

        request.state.tenant = tenant_context
        response = await call_next(request)
        # Echo resolved tenant ID for API gateway correlation.
        response.headers["X-Tenant-ID"] = tenant_id
        return response


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


async def _validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Returns structured validation errors for malformed request payloads."""
    return _json_error(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        error_code="validation_error",
        message="Request payload failed schema validation.",
        request_id=getattr(request.state, "request_id", None),
        details=exc.errors(),
    )


async def _http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Wraps FastAPI HTTPException in the unified ErrorEnvelope."""
    return _json_error(
        status_code=exc.status_code,
        error_code=f"http_{exc.status_code}",
        message=exc.detail or "An HTTP error occurred.",
        request_id=getattr(request.state, "request_id", None),
    )


async def _tenant_not_found_handler(
    request: Request, exc: TenantNotFoundError
) -> JSONResponse:
    return _json_error(
        status_code=status.HTTP_404_NOT_FOUND,
        error_code="tenant_not_found",
        message=str(exc),
        request_id=getattr(request.state, "request_id", None),
    )


async def _tenant_inactive_handler(
    request: Request, exc: TenantInactiveError
) -> JSONResponse:
    return _json_error(
        status_code=status.HTTP_403_FORBIDDEN,
        error_code="tenant_inactive",
        message=str(exc),
        request_id=getattr(request.state, "request_id", None),
    )


async def _unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """
    Catch-all for unhandled exceptions.
    Logs with full traceback; returns a safe opaque message to the client.
    """
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception(
        '"Unhandled exception [request_id=%s]: %s"',
        request_id,
        exc,
    )
    return _json_error(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code="internal_server_error",
        message="An unexpected internal error occurred. Please contact support with the request_id.",
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_error(
    *,
    status_code: int,
    error_code: str,
    message: str,
    request_id: str | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope(
        error_code=error_code,
        message=message,
        request_id=request_id,
        details=details,
    )
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(exclude_none=False),
    )


def get_tenant_from_request(request: Request) -> TenantContext:
    """
    Convenience accessor for downstream endpoint functions.

    Usage:
        @router.get("/my-endpoint")
        async def my_endpoint(request: Request):
            tenant = get_tenant_from_request(request)
            # tenant.sla.max_concurrent_jobs is now safely accessible.

    Raises RuntimeError if TenantMiddleware has not run (e.g., exempt path).
    """
    tenant: TenantContext | None = getattr(request.state, "tenant", None)
    if tenant is None:
        raise RuntimeError(
            "TenantContext not found in request.state. "
            "Ensure TenantMiddleware is registered and the path is not exempt."
        )
    return tenant


# ---------------------------------------------------------------------------
# Application instance
# ---------------------------------------------------------------------------

app: FastAPI = create_application()
