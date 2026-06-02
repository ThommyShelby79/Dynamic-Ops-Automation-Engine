"""
app/core/config_manager.py

Unified Configuration Manager — Dynamic Ops Automation Engine
Responsibilities:
  1. Load base application settings from environment variables (.env).
  2. Resolve and cache per-tenant TenantContext objects from a pluggable backend.
  3. Provide a synchronous fallback for non-async contexts (startup hooks, etc.).
  4. Expose a structured AppSettings Pydantic model for all non-tenant globals.

Design constraints:
  - ZERO hardcoded tenant values anywhere in this file.
  - All secrets (DB passwords, signing keys) sourced ONLY from environment variables
    or an injected secrets provider, never from tenant config payloads.
  - Thread-safe in-process LRU cache for TenantContext to avoid per-request I/O.
  - The TenantRegistry abstraction allows swapping between env-file, database,
    and external secrets-manager backends without touching call sites.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Annotated, Any, Final

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.models.tenant import (
    ErlangThresholds,
    SLAConfig,
    TenantContext,
    TenantTier,
    WebhookAuthScheme,
    WebhookDestination,
    build_sla_for_tier,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_ENV_FILE: Final[str] = ".env"
_TENANT_CACHE_TTL_SECONDS: Final[float] = 300.0  # 5 minutes
_TENANT_CACHE_MAX_SIZE: Final[int] = 1_024


# ---------------------------------------------------------------------------
# Application-level settings (non-tenant globals)
# ---------------------------------------------------------------------------


class DatabaseSettings(BaseModel):
    """Relational database connection parameters."""

    model_config = ConfigDict(frozen=True)

    host: str
    port: Annotated[int, Field(ge=1, le=65_535)]
    name: str
    user: str
    password: SecretStr
    pool_min_size: Annotated[int, Field(ge=1, le=100)] = 2
    pool_max_size: Annotated[int, Field(ge=1, le=500)] = 20
    pool_timeout_seconds: Annotated[float, Field(gt=0.0, le=60.0)] = 10.0
    ssl_enabled: bool = True

    @property
    def async_dsn(self) -> str:
        """asyncpg / SQLAlchemy async DSN."""
        return (
            f"postgresql+asyncpg://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )

    @property
    def sync_dsn(self) -> str:
        """psycopg2 / SQLAlchemy sync DSN (for Alembic migrations)."""
        return (
            f"postgresql+psycopg2://{self.user}:{self.password.get_secret_value()}"
            f"@{self.host}:{self.port}/{self.name}"
        )


class RedisSettings(BaseModel):
    """Redis connection parameters for caching and async job queues."""

    model_config = ConfigDict(frozen=True)

    host: str = "localhost"
    port: Annotated[int, Field(ge=1, le=65_535)] = 6379
    db: Annotated[int, Field(ge=0, le=15)] = 0
    password: SecretStr | None = None
    ssl_enabled: bool = False
    max_connections: Annotated[int, Field(ge=1, le=10_000)] = 100
    socket_timeout_seconds: Annotated[float, Field(gt=0.0, le=60.0)] = 5.0

    @property
    def url(self) -> str:
        scheme = "rediss" if self.ssl_enabled else "redis"
        auth = (
            f":{self.password.get_secret_value()}@"
            if self.password
            else ""
        )
        return f"{scheme}://{auth}{self.host}:{self.port}/{self.db}"


class ObservabilitySettings(BaseModel):
    """Distributed tracing, metrics export, and structured log configuration."""

    model_config = ConfigDict(frozen=True)

    otlp_endpoint: AnyHttpUrl | None = None
    metrics_push_interval_seconds: Annotated[float, Field(gt=0.0)] = 15.0
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "text"
    sentry_dsn: SecretStr | None = None
    sentry_traces_sample_rate: Annotated[float, Field(ge=0.0, le=1.0)] = 0.05

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, v: Any) -> str:
        normalized = str(v).upper()
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if normalized not in valid:
            raise ValueError(f"log_level must be one of {valid}, got '{v}'.")
        return normalized


class AppSettings(BaseSettings):
    """
    Root application settings loaded from environment variables.

    Precedence (highest to lowest):
      1. Actual environment variables
      2. Variables in the .env file specified by ENV_FILE path
      3. Field defaults defined here

    All secrets are typed as SecretStr to prevent accidental logging.
    """

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", _DEFAULT_ENV_FILE),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Runtime identity ---
    app_name: str = Field(default="dynamic-ops-engine", alias="APP_NAME")
    environment: Annotated[
        str,
        Field(alias="ENVIRONMENT", description="One of: local, development, staging, production"),
    ] = "local"
    debug: bool = Field(default=False, alias="DEBUG")
    version: str = Field(default="0.1.0", alias="APP_VERSION")

    # --- API server ---
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: Annotated[int, Field(ge=1, le=65_535, alias="API_PORT")] = 8000
    api_workers: Annotated[int, Field(ge=1, le=64, alias="API_WORKERS")] = 1
    api_root_path: str = Field(default="", alias="API_ROOT_PATH")
    cors_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000"],
        alias="CORS_ALLOWED_ORIGINS",
    )

    # --- Security ---
    secret_key: SecretStr = Field(alias="SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    jwt_access_token_expire_minutes: Annotated[
        int, Field(ge=1, alias="JWT_ACCESS_TOKEN_EXPIRE_MINUTES")
    ] = 30

    # --- Tenant resolution ---
    tenant_registry_backend: Annotated[
        str,
        Field(
            alias="TENANT_REGISTRY_BACKEND",
            description="Backend type: 'env_file' | 'database' | 'secrets_manager'",
        ),
    ] = "env_file"
    tenant_config_dir: Path = Field(
        default=Path("config/tenants"),
        alias="TENANT_CONFIG_DIR",
        description=(
            "Directory containing per-tenant JSON config files when using the "
            "'env_file' registry backend. Ignored for other backends."
        ),
    )
    tenant_cache_ttl_seconds: float = Field(
        default=_TENANT_CACHE_TTL_SECONDS,
        alias="TENANT_CACHE_TTL_SECONDS",
    )

    # --- Database ---
    db_host: str = Field(default="localhost", alias="DB_HOST")
    db_port: int = Field(default=5432, alias="DB_PORT")
    db_name: str = Field(default="ops_engine", alias="DB_NAME")
    db_user: str = Field(default="postgres", alias="DB_USER")
    db_password: SecretStr = Field(alias="DB_PASSWORD")
    db_pool_min: int = Field(default=2, alias="DB_POOL_MIN")
    db_pool_max: int = Field(default=20, alias="DB_POOL_MAX")

    # --- Redis ---
    redis_host: str = Field(default="localhost", alias="REDIS_HOST")
    redis_port: int = Field(default=6379, alias="REDIS_PORT")
    redis_db: int = Field(default=0, alias="REDIS_DB")
    redis_password: SecretStr | None = Field(default=None, alias="REDIS_PASSWORD")
    redis_ssl: bool = Field(default=False, alias="REDIS_SSL")

    # --- Observability ---
    otlp_endpoint: str | None = Field(default=None, alias="OTLP_ENDPOINT")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="json", alias="LOG_FORMAT")
    sentry_dsn: SecretStr | None = Field(default=None, alias="SENTRY_DSN")

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, v: Any) -> str:
        valid = {"local", "development", "staging", "production"}
        normalized = str(v).lower().strip()
        if normalized not in valid:
            raise ValueError(f"ENVIRONMENT must be one of {valid}, got '{v}'.")
        return normalized

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        """Allow comma-separated string from env or a proper JSON list."""
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            if v.startswith("["):
                return json.loads(v)
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        raise ValueError("CORS_ALLOWED_ORIGINS must be a list or comma-separated string.")

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def database(self) -> DatabaseSettings:
        return DatabaseSettings(
            host=self.db_host,
            port=self.db_port,
            name=self.db_name,
            user=self.db_user,
            password=self.db_password,
            pool_min_size=self.db_pool_min,
            pool_max_size=self.db_pool_max,
        )

    @property
    def redis(self) -> RedisSettings:
        return RedisSettings(
            host=self.redis_host,
            port=self.redis_port,
            db=self.redis_db,
            password=self.redis_password,
            ssl_enabled=self.redis_ssl,
        )

    @property
    def observability(self) -> ObservabilitySettings:
        return ObservabilitySettings(
            otlp_endpoint=self.otlp_endpoint,  # type: ignore[arg-type]
            log_level=self.log_level,
            log_format=self.log_format,
            sentry_dsn=self.sentry_dsn,
        )


# ---------------------------------------------------------------------------
# Tenant registry abstraction
# ---------------------------------------------------------------------------


class TenantNotFoundError(KeyError):
    """Raised when a tenant_id cannot be resolved from the registry."""

    def __init__(self, tenant_id: str) -> None:
        super().__init__(f"Tenant '{tenant_id}' not found in registry.")
        self.tenant_id = tenant_id


class TenantInactiveError(PermissionError):
    """Raised when a resolved tenant has is_active=False."""

    def __init__(self, tenant_id: str) -> None:
        super().__init__(f"Tenant '{tenant_id}' is inactive and cannot process requests.")
        self.tenant_id = tenant_id


class BaseTenantRegistry(ABC):
    """
    Abstract interface for tenant resolution backends.
    Implementations must be async-safe and must NOT cache internally;
    caching is handled by ConfigManager to keep TTL logic in one place.
    """

    @abstractmethod
    async def fetch(self, tenant_id: str) -> TenantContext:
        """
        Fetch and return the TenantContext for the given tenant_id.
        Must raise TenantNotFoundError if the tenant does not exist.
        Must NOT check is_active — that is the caller's responsibility.
        """
        ...

    @abstractmethod
    async def list_tenant_ids(self) -> list[str]:
        """Return all known tenant IDs. Used for startup validation and admin APIs."""
        ...

    async def health_check(self) -> bool:
        """Return True if the registry backend is reachable. Override if applicable."""
        return True


class EnvFileTenantRegistry(BaseTenantRegistry):
    """
    Loads tenant configurations from JSON files on disk.

    Expected layout:
      {tenant_config_dir}/
        {tenant_id}.json   ← one file per tenant

    JSON schema mirrors TenantContext.model_fields. The file must be a valid
    JSON object; all unrecognized keys are ignored by TenantContext's ConfigDict.

    This backend is appropriate for local development and single-node staging.
    Production deployments must use the DatabaseTenantRegistry.
    """

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = config_dir
        if not self._config_dir.exists():
            logger.warning(
                "Tenant config directory '%s' does not exist. "
                "Creating it now; populate with <tenant_id>.json files.",
                self._config_dir,
            )
            self._config_dir.mkdir(parents=True, exist_ok=True)

    async def fetch(self, tenant_id: str) -> TenantContext:
        path = self._config_dir / f"{tenant_id}.json"
        if not path.exists():
            raise TenantNotFoundError(tenant_id)
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            payload: dict[str, Any] = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise TenantNotFoundError(tenant_id) from exc

        return self._build_context(tenant_id, payload)

    async def list_tenant_ids(self) -> list[str]:
        files = await asyncio.to_thread(
            lambda: list(self._config_dir.glob("*.json"))
        )
        return [f.stem for f in files]

    def _build_context(self, tenant_id: str, payload: dict[str, Any]) -> TenantContext:
        """
        Merge payload from disk with tier-based SLA defaults.
        Explicit SLA fields in the JSON override the tier defaults.
        """
        tier_raw = payload.get("tier", TenantTier.STANDARD.value)
        tier = TenantTier(tier_raw)

        sla_overrides: dict[str, Any] = payload.get("sla_overrides", {})
        sla = build_sla_for_tier(tier, sla_overrides)

        erlang_raw: dict[str, Any] = payload.get("erlang", {})
        erlang = ErlangThresholds(**erlang_raw) if erlang_raw else ErlangThresholds()

        webhooks_raw: list[dict[str, Any]] = payload.get("webhooks", [])
        webhooks = [WebhookDestination(**wh) for wh in webhooks_raw]

        return TenantContext(
            tenant_id=tenant_id,
            tenant_name=payload.get("tenant_name", tenant_id),
            tier=tier,
            is_active=payload.get("is_active", True),
            sla=sla,
            erlang=erlang,
            webhooks=webhooks,
            metadata=payload.get("metadata", {}),
        )


# ---------------------------------------------------------------------------
# In-process TTL cache for TenantContext
# ---------------------------------------------------------------------------


class _CacheEntry:
    __slots__ = ("context", "fetched_at")

    def __init__(self, context: TenantContext) -> None:
        self.context = context
        self.fetched_at: float = time.monotonic()

    def is_expired(self, ttl: float) -> bool:
        return (time.monotonic() - self.fetched_at) >= ttl


class _TenantContextCache:
    """
    Thread-safe, TTL-backed in-process cache for TenantContext objects.
    Bounded by _TENANT_CACHE_MAX_SIZE (LRU eviction via insertion-order dict).
    """

    def __init__(self, ttl_seconds: float, max_size: int) -> None:
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._store: dict[str, _CacheEntry] = {}
        self._lock = Lock()

    def get(self, tenant_id: str) -> TenantContext | None:
        with self._lock:
            entry = self._store.get(tenant_id)
            if entry is None:
                return None
            if entry.is_expired(self._ttl):
                del self._store[tenant_id]
                return None
            return entry.context

    def set(self, tenant_id: str, context: TenantContext) -> None:
        with self._lock:
            # Evict oldest entry if at capacity.
            if tenant_id not in self._store and len(self._store) >= self._max_size:
                oldest_key = next(iter(self._store))
                del self._store[oldest_key]
                logger.debug("Tenant cache evicted LRU entry: %s", oldest_key)
            self._store[tenant_id] = _CacheEntry(context)

    def invalidate(self, tenant_id: str) -> None:
        with self._lock:
            self._store.pop(tenant_id, None)

    def invalidate_all(self) -> None:
        with self._lock:
            self._store.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._store)


# ---------------------------------------------------------------------------
# ConfigManager — singleton orchestrator
# ---------------------------------------------------------------------------


class ConfigManager:
    """
    Central configuration authority for the Dynamic Ops Engine.

    Lifecycle:
      - Instantiate once at application startup via get_config_manager().
      - Inject into FastAPI's dependency system via Depends(get_config_manager).
      - Call .initialize() during the lifespan startup hook.
      - Call .shutdown() during the lifespan shutdown hook.

    Thread/async safety:
      - All async public methods are safe for concurrent use.
      - The tenant cache uses a threading.Lock for synchronous safe access.
      - Heavy I/O (file reads, DB queries) is offloaded via asyncio.to_thread.
    """

    def __init__(
        self,
        settings: AppSettings,
        registry: BaseTenantRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._registry: BaseTenantRegistry = registry or self._build_registry(settings)
        self._cache = _TenantContextCache(
            ttl_seconds=settings.tenant_cache_ttl_seconds,
            max_size=_TENANT_CACHE_MAX_SIZE,
        )
        self._initialized = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """
        Perform startup validation:
          - Verify registry backend connectivity.
          - Pre-warm cache for all tenants (best-effort; failures are logged, not raised).
        """
        logger.info(
            "ConfigManager initializing (backend=%s, env=%s)",
            self._settings.tenant_registry_backend,
            self._settings.environment,
        )
        backend_healthy = await self._registry.health_check()
        if not backend_healthy:
            raise RuntimeError(
                f"Tenant registry backend '{self._settings.tenant_registry_backend}' "
                "failed its health check during startup."
            )
        try:
            tenant_ids = await self._registry.list_tenant_ids()
            logger.info("Pre-warming tenant cache for %d tenants.", len(tenant_ids))
            tasks = [self._warm_tenant(tid) for tid in tenant_ids]
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tenant cache pre-warm encountered errors: %s", exc)
        self._initialized = True
        logger.info("ConfigManager initialized successfully.")

    async def shutdown(self) -> None:
        self._cache.invalidate_all()
        self._initialized = False
        logger.info("ConfigManager shut down; tenant cache cleared.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def settings(self) -> AppSettings:
        """Read-only access to application-level settings."""
        return self._settings

    async def get_tenant(self, tenant_id: str) -> TenantContext:
        """
        Resolve a TenantContext by tenant_id.

        Resolution order:
          1. In-process TTL cache (sub-millisecond).
          2. Registry backend (file, DB, secrets manager).

        Raises:
          TenantNotFoundError: If the tenant_id is unknown.
          TenantInactiveError: If the tenant exists but is_active=False.
        """
        cached = self._cache.get(tenant_id)
        if cached is not None:
            logger.debug("Tenant cache HIT: %s", tenant_id)
            if not cached.is_active:
                raise TenantInactiveError(tenant_id)
            return cached

        logger.debug("Tenant cache MISS: %s — fetching from registry.", tenant_id)
        context = await self._registry.fetch(tenant_id)
        self._cache.set(tenant_id, context)

        if not context.is_active:
            raise TenantInactiveError(tenant_id)
        return context

    async def invalidate_tenant_cache(self, tenant_id: str) -> None:
        """
        Evict a specific tenant from the cache, forcing the next request to
        re-fetch from the registry backend. Use after admin config mutations.
        """
        self._cache.invalidate(tenant_id)
        logger.info("Tenant cache invalidated for: %s", tenant_id)

    async def list_tenants(self) -> list[str]:
        """Return all known tenant IDs from the registry backend."""
        return await self._registry.list_tenant_ids()

    def cache_stats(self) -> dict[str, Any]:
        """Return diagnostic information about the current cache state."""
        return {
            "size": self._cache.size(),
            "max_size": _TENANT_CACHE_MAX_SIZE,
            "ttl_seconds": self._settings.tenant_cache_ttl_seconds,
            "backend": self._settings.tenant_registry_backend,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _warm_tenant(self, tenant_id: str) -> None:
        try:
            context = await self._registry.fetch(tenant_id)
            self._cache.set(tenant_id, context)
            logger.debug("Pre-warmed tenant: %s", tenant_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to pre-warm tenant '%s': %s", tenant_id, exc)

    @staticmethod
    def _build_registry(settings: AppSettings) -> BaseTenantRegistry:
        backend = settings.tenant_registry_backend
        if backend == "env_file":
            return EnvFileTenantRegistry(config_dir=settings.tenant_config_dir)
        # Future backends:
        # elif backend == "database":
        #     return DatabaseTenantRegistry(dsn=settings.database.async_dsn)
        # elif backend == "secrets_manager":
        #     return SecretsManagerTenantRegistry(...)
        raise ValueError(
            f"Unknown TENANT_REGISTRY_BACKEND='{backend}'. "
            "Valid values: 'env_file', 'database', 'secrets_manager'."
        )


# ---------------------------------------------------------------------------
# Application-level singleton and dependency injection helpers
# ---------------------------------------------------------------------------

_config_manager_instance: ConfigManager | None = None
_init_lock = asyncio.Lock()


@lru_cache(maxsize=1)
def get_app_settings() -> AppSettings:
    """
    Load and cache AppSettings exactly once per process.
    The @lru_cache ensures environment variable reads happen only at first call.
    Call invalidate on test teardown if settings need to be reloaded.
    """
    return AppSettings()  # type: ignore[call-arg]


async def get_config_manager() -> ConfigManager:
    """
    FastAPI dependency that returns the initialized ConfigManager singleton.

    Usage in endpoint:
        async def endpoint(cm: ConfigManager = Depends(get_config_manager)):
            tenant = await cm.get_tenant(tenant_id)

    The double-checked lock pattern ensures initialize() runs exactly once
    even under concurrent startup requests.
    """
    global _config_manager_instance
    if _config_manager_instance is not None:
        return _config_manager_instance
    async with _init_lock:
        if _config_manager_instance is None:
            settings = get_app_settings()
            manager = ConfigManager(settings=settings)
            await manager.initialize()
            _config_manager_instance = manager
    return _config_manager_instance


async def reset_config_manager() -> None:
    """
    Tear down and clear the singleton. Used in test fixtures and shutdown hooks.
    """
    global _config_manager_instance
    if _config_manager_instance is not None:
        await _config_manager_instance.shutdown()
        _config_manager_instance = None
    get_app_settings.cache_clear()


@asynccontextmanager
async def config_manager_lifespan() -> AsyncIterator[ConfigManager]:
    """
    Async context manager for use in FastAPI lifespan hooks.

    Usage in app/main.py:
        async with config_manager_lifespan() as cm:
            app.state.config_manager = cm
            yield
    """
    manager = await get_config_manager()
    try:
        yield manager
    finally:
        await reset_config_manager()
