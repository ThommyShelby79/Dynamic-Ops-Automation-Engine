"""
app/api/v1/routers/ingestion.py

Ingestion Router — Dynamic Ops Automation Engine
Phase 8 Final: Fully non-blocking async ingestion pattern across all three pipelines.

Pipeline architecture:
  ┌─────────────┬──────────────────────────────────┬──────────────────────────────────┬──────────┐
  │ Endpoint    │ Synchronous (request cycle)       │ Async (background / thread pool) │ Response │
  ├─────────────┼──────────────────────────────────┼──────────────────────────────────┼──────────┤
  │ /forecast   │ Normalize → Erlang C staffing     │ — (CPU-bound via to_thread)      │ 200 OK   │
  │             │ (offloaded to thread pool via      │                                  │          │
  │             │  asyncio.to_thread to avoid        │                                  │          │
  │             │  blocking the event loop)          │                                  │          │
  ├─────────────┼──────────────────────────────────┼──────────────────────────────────┼──────────┤
  │ /kpi        │ Normalize → threshold evaluation  │ Webhook fan-out via              │ 202      │
  │             │ (pure Python, fast, in-cycle)      │ BackgroundTasks                  │ Accepted │
  ├─────────────┼──────────────────────────────────┼──────────────────────────────────┼──────────┤
  │ /adherence  │ Normalize                         │ DB persist via                   │ 202      │
  │             │ (fast, in-cycle)                  │ BackgroundTasks                  │ Accepted │
  └─────────────┴──────────────────────────────────┴──────────────────────────────────┴──────────┘

Design constraints:
  - ZERO blocking I/O or CPU-heavy work on the asyncio event loop.
  - All three endpoints are secured by verify_api_key (Phase 7.2).
  - BackgroundTasks is injected by FastAPI's DI system — never instantiated manually.
  - The inline import of storage_tasks inside ingest_adherence is promoted to a
    module-level import here for clarity and import-time failure detection.
  - asyncio.to_thread is used for the Erlang C computation because WFMEngine is a
    pure synchronous-style CPU loop; wrapping it preserves event loop responsiveness
    under concurrent forecast requests without requiring a separate process pool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import JSONResponse

from app.api.v1.dependencies.security import verify_api_key
from app.core.models.contracts import (
    AdherenceEventPayload,
    KPIHealthPayload,
    VolumeForecastPayload,
)
from app.core.models.tenant import TenantContext
from app.services.data_normalizer import DataNormalizer, NormalizationError
from app.services.sentinel_engine import SentinelEngine
from app.services.storage_tasks import persist_adherence_event
from app.services.webhook_dispatcher import WebhookDispatcher
from app.services.wfm_engine import WFMEngine

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ingestion",
    tags=["Ingestion"],
)

# ---------------------------------------------------------------------------
# Shared dispatcher instance.
# WebhookDispatcher is stateless (no connection pool, no mutable fields), so a
# single module-level instance is safe to share across all concurrent requests
# and background tasks.
# ---------------------------------------------------------------------------
_dispatcher = WebhookDispatcher()


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _ok(data: dict[str, Any], request_id: str | None = None) -> JSONResponse:
    """Construct a 200 OK JSONResponse envelope."""
    body: dict[str, Any] = {"status": "ok", "data": data}
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(status_code=status.HTTP_200_OK, content=body)


def _accepted(data: dict[str, Any], request_id: str | None = None) -> JSONResponse:
    """Construct a 202 Accepted JSONResponse envelope for fire-and-forget pipelines."""
    body: dict[str, Any] = {"status": "accepted", "data": data}
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=body)


def _error(
    http_status: int,
    error_code: str,
    message: str,
    request_id: str | None = None,
    details: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """Construct a structured error JSONResponse envelope."""
    body: dict[str, Any] = {
        "status": "error",
        "error_code": error_code,
        "message": message,
    }
    if request_id:
        body["request_id"] = request_id
    if details:
        body["details"] = details
    return JSONResponse(status_code=http_status, content=body)


# ---------------------------------------------------------------------------
# /forecast — WFM Erlang C staffing
# ---------------------------------------------------------------------------

@router.post(
    "/forecast",
    summary="Ingest a volume forecast payload",
    description=(
        "Normalises the raw payload and computes Erlang C staffing predictions. "
        "The CPU-bound staffing computation is offloaded to a thread pool via "
        "asyncio.to_thread to keep the event loop free for concurrent requests. "
        "Returns staffing predictions synchronously in the response body. "
        "Requires a valid X-API-Key for the resolved tenant."
    ),
    status_code=status.HTTP_200_OK,
)
async def ingest_forecast(
    request: Request,
    raw_payload: dict[str, Any],
    tenant: Annotated[TenantContext, Depends(verify_api_key)],
) -> JSONResponse:
    """
    Pipeline:
      raw dict ──► DataNormalizer.clean_forecast_data ──► VolumeForecastPayload
               ──► asyncio.to_thread(WFMEngine.calculate_staffing) ──► 200 OK
    """
    request_id: str | None = getattr(request.state, "request_id", None)

    # ── 1. Normalize and validate (fast, pure Python, safe on event loop) ──
    try:
        payload: VolumeForecastPayload = await DataNormalizer.clean_forecast_data(
            raw_payload
        )
    except NormalizationError as exc:
        logger.warning(
            "Forecast normalization failed [tenant=%s, request_id=%s]: %s",
            tenant.tenant_id,
            request_id,
            exc,
        )
        return _error(
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="normalization_error",
            message=str(exc),
            request_id=request_id,
            details=exc.errors,
        )

    # ── 2. Erlang C computation — offloaded to thread pool ──────────────────
    # WFMEngine.calculate_staffing is declared `async` but its internal loop
    # (_required_agents iterating until service level is met) is CPU-bound.
    # Wrapping in asyncio.to_thread prevents it from stalling the event loop
    # under large historical_volumes arrays or high concurrency.
    #
    # asyncio.to_thread runs the coroutine by scheduling the synchronous
    # wrapper in the default ThreadPoolExecutor. Because calculate_staffing
    # is already `async`, we call it directly here — if the computation ever
    # grows heavy enough to warrant a process pool, swap to
    # loop.run_in_executor(process_pool_executor, ...) at that point.
    result: dict[str, Any] = await asyncio.to_thread(
        # asyncio.to_thread requires a plain callable, so we wrap the coroutine
        # call. For a truly async method, awaiting it inside to_thread is not
        # correct — we invoke the synchronous compute path directly.
        _run_staffing_sync,
        payload,
        tenant,
    )

    logger.info(
        "Forecast computed [tenant=%s, volumes=%d, request_id=%s]",
        tenant.tenant_id,
        len(payload.historical_volumes),
        request_id,
    )
    return _ok(data=result, request_id=request_id)


def _run_staffing_sync(
    payload: VolumeForecastPayload,
    tenant: TenantContext,
) -> dict[str, Any]:
    """
    Pure synchronous wrapper around the Erlang C staffing logic.

    asyncio.to_thread requires a plain callable (not a coroutine). Since
    WFMEngine's actual compute work is synchronous Python math (no await
    statements inside the hot loop), we replicate the logic here as a
    synchronous function that to_thread can safely execute in the thread
    pool without touching the event loop.

    If WFMEngine.calculate_staffing ever gains real async I/O (e.g. DB
    lookups for historical benchmarks), remove this wrapper and instead
    use asyncio.run_coroutine_threadsafe or restructure the call site.
    """
    import math

    interval_min = payload.interval_minutes
    aht_sec = tenant.erlang.average_call_duration_seconds
    target_sl_frac = tenant.erlang.target_service_level_percent / 100.0
    factor = aht_sec / (interval_min * 60.0)

    def erlang_b(N: int, A: float) -> float:
        if A == 0:
            return 0.0
        B = 1.0
        for i in range(1, N + 1):
            B = (A * B) / (i + A * B)
        return B

    def erlang_c(N: int, A: float) -> float:
        if N <= A:
            return 1.0
        B = erlang_b(N, A)
        return min(B / (1.0 - (A / N) * (1.0 - B)), 1.0)

    def required_agents(A: float) -> int:
        N = math.ceil(A) or 1
        while N <= 10_000:
            if (1.0 - erlang_c(N, A)) >= target_sl_frac:
                return N
            N += 1
        return 10_000

    predictions = []
    for vol in payload.historical_volumes:
        if vol <= 0:
            predictions.append({
                "volume": vol,
                "agents_required": 0,
                "predicted_service_level": 100.0,
            })
        else:
            A = vol * factor
            n = required_agents(A)
            sl = round((1.0 - erlang_c(n, A)) * 100.0, 2)
            predictions.append({
                "volume": vol,
                "agents_required": n,
                "predicted_service_level": sl,
            })

    return {
        "interval_minutes": interval_min,
        "aht_seconds": aht_sec,
        "target_service_level": tenant.erlang.target_service_level_percent,
        "predictions": predictions,
    }


# ---------------------------------------------------------------------------
# /kpi — CX KPI health + background webhook dispatch
# ---------------------------------------------------------------------------

@router.post(
    "/kpi",
    summary="Ingest a KPI health snapshot",
    description=(
        "Normalises the payload and evaluates KPI thresholds synchronously. "
        "Any breach events are queued for webhook fan-out via BackgroundTasks — "
        "the client receives 202 Accepted immediately without waiting for HTTP "
        "delivery to downstream webhook destinations. "
        "Requires a valid X-API-Key for the resolved tenant."
    ),
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_kpi(
    request: Request,
    background_tasks: BackgroundTasks,
    raw_payload: dict[str, Any],
    tenant: Annotated[TenantContext, Depends(verify_api_key)],
) -> JSONResponse:
    """
    Pipeline:
      raw dict ──► DataNormalizer.clean_kpi_data ──► KPIHealthPayload
               ──► SentinelEngine.evaluate_kpi_health (threshold eval, in-cycle)
               ──► background_tasks.add_task(dispatcher.dispatch_event) per breach
               ──► 202 Accepted (client unblocked before any webhook HTTP I/O)
    """
    request_id: str | None = getattr(request.state, "request_id", None)

    # ── 1. Normalize and validate ────────────────────────────────────────────
    try:
        payload: KPIHealthPayload = await DataNormalizer.clean_kpi_data(raw_payload)
    except NormalizationError as exc:
        logger.warning(
            "KPI normalization failed [tenant=%s, request_id=%s]: %s",
            tenant.tenant_id,
            request_id,
            exc,
        )
        return _error(
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="normalization_error",
            message=str(exc),
            request_id=request_id,
            details=exc.errors,
        )

    # ── 2. Threshold evaluation + background webhook dispatch ────────────────
    # SentinelEngine.evaluate_kpi_health accepts BackgroundTasks and internally
    # calls background_tasks.add_task(dispatcher.dispatch_event, event, tenant)
    # for each breached threshold. No webhook HTTP I/O occurs in this cycle.
    result: dict[str, Any] = await SentinelEngine.evaluate_kpi_health(
        payload=payload,
        tenant=tenant,
        dispatcher=_dispatcher,
        background_tasks=background_tasks,
    )

    logger.info(
        "KPI ingested [tenant=%s, client=%s, sentinel_status=%s, "
        "events_queued=%d, request_id=%s]",
        tenant.tenant_id,
        payload.client_id,
        result.get("status"),
        len(result.get("events_dispatched", [])),
        request_id,
    )
    return _accepted(data=result, request_id=request_id)


# ---------------------------------------------------------------------------
# /adherence — RTA floor adherence event + background persistence
# ---------------------------------------------------------------------------

@router.post(
    "/adherence",
    summary="Ingest an RTA floor adherence event",
    description=(
        "Normalises the raw adherence event payload and queues it for background "
        "persistence via BackgroundTasks. The client receives 202 Accepted "
        "immediately; DB I/O occurs after the response is sent. "
        "Requires a valid X-API-Key for the resolved tenant."
    ),
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_adherence(
    request: Request,
    background_tasks: BackgroundTasks,
    raw_payload: dict[str, Any],
    tenant: Annotated[TenantContext, Depends(verify_api_key)],
) -> JSONResponse:
    """
    Pipeline:
      raw dict ──► DataNormalizer.clean_adherence_data ──► AdherenceEventPayload
               ──► background_tasks.add_task(persist_adherence_event)
               ──► 202 Accepted (client unblocked before any DB I/O)
    """
    request_id: str | None = getattr(request.state, "request_id", None)

    # ── 1. Normalize and validate ────────────────────────────────────────────
    try:
        payload: AdherenceEventPayload = await DataNormalizer.clean_adherence_data(
            raw_payload
        )
    except NormalizationError as exc:
        logger.warning(
            "Adherence normalization failed [tenant=%s, request_id=%s]: %s",
            tenant.tenant_id,
            request_id,
            exc,
        )
        return _error(
            http_status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            error_code="normalization_error",
            message=str(exc),
            request_id=request_id,
            details=exc.errors,
        )

    # ── 2. Queue persistence — fire and forget ───────────────────────────────
    background_tasks.add_task(persist_adherence_event, payload, tenant.tenant_id)

    logger.info(
        "Adherence event queued [tenant=%s, agent=%s, state=%s, request_id=%s]",
        tenant.tenant_id,
        payload.agent_id,
        payload.state_slug,
        request_id,
    )
    return _accepted(
        data={
            "agent_id": payload.agent_id,
            "state_slug": payload.state_slug,
            "duration_seconds": payload.duration_seconds,
            "timestamp": payload.timestamp.isoformat(),
            "status": "queued_for_storage",
        },
        request_id=request_id,
    )
