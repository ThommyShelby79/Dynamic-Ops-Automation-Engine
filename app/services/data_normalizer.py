from typing import Any, Dict, Set

from pydantic import ValidationError

from app.core.models.contracts import (
    AdherenceEventPayload,
    KPIHealthPayload,
    VolumeForecastPayload,
)


class NormalizationError(Exception):
    """Raised when raw data cannot be cleaned / validated against a schema."""

    def __init__(self, message: str, errors: Any = None) -> None:
        super().__init__(message)
        self.errors = errors


class DataNormalizer:
    """
    Utility class that transforms messy, legacy‑style dictionaries into
    strict Pydantic v2 models.  All classmethods are async to fit into
    an async ingestion pipeline.
    """

    # ── key maps for cleaning raw CSV/API field names ───────────────
    FORECAST_KEY_MAP = {
        "interval_minutes": "interval_minutes",
        "interval minutes": "interval_minutes",
        "interval_mins": "interval_minutes",
        "intervalminutes": "interval_minutes",
        "aht": "aht",
        "average_handling_time": "aht",
        "avg_handle_time": "aht",
        "historical_volumes": "historical_volumes",
        "historical volumes": "historical_volumes",
        "volumes": "historical_volumes",
        "volume": "historical_volumes",
    }

    KPI_KEY_MAP = {
        "client_id": "client_id",
        "client id": "client_id",
        "clientid": "client_id",
        "timestamp": "timestamp",
        "csat": "csat",
        "csat_score": "csat",
        "csatscore": "csat",
        "sla_percent": "sla_percent",
        "sla": "sla_percent",
        "sla_percentage": "sla_percent",
        "fcr_percent": "fcr_percent",
        "fcr": "fcr_percent",
        "fcr_percentage": "fcr_percent",
        "rolling_window_days": "rolling_window_days",
        "rolling window": "rolling_window_days",
        "rollingwindow": "rolling_window_days",
        "window_days": "rolling_window_days",
    }

    ADHERENCE_KEY_MAP = {
        "agent_id": "agent_id",
        "agent id": "agent_id",
        "agentid": "agent_id",
        "state_slug": "state_slug",
        "state": "state_slug",
        "state slug": "state_slug",
        "stateslug": "state_slug",
        "duration_seconds": "duration_seconds",
        "duration": "duration_seconds",
        "duration seconds": "duration_seconds",
        "duration_secs": "duration_seconds",
        "timestamp": "timestamp",
    }

    # ── private helpers ─────────────────────────────────────────────
    @staticmethod
    def _normalize_keys(
        raw: Dict[str, Any],
        key_map: Dict[str, str],
        valid_fields: Set[str],
    ) -> Dict[str, Any]:
        """
        Normalise raw dictionary keys using the provided mapping and
        discard any key that does not map to a valid model field.
        """
        cleaned: Dict[str, Any] = {}
        for raw_key, value in raw.items():
            norm = raw_key.strip().lower().replace(" ", "_")
            mapped = key_map.get(raw_key) or key_map.get(norm)
            if mapped and mapped in valid_fields:
                cleaned[mapped] = value
            elif norm in valid_fields:
                cleaned[norm] = value
        return cleaned

    # ── public classmethods ─────────────────────────────────────────
    @classmethod
    async def clean_forecast_data(
        cls, raw: Dict[str, Any]
    ) -> VolumeForecastPayload:
        """Map, coerce and validate a raw dict into a VolumeForecastPayload."""
        try:
            fields = set(VolumeForecastPayload.model_fields.keys())
            cleaned = cls._normalize_keys(raw, cls.FORECAST_KEY_MAP, fields)

            # Coerce common string representations
            if "interval_minutes" in cleaned and isinstance(
                cleaned["interval_minutes"], str
            ):
                cleaned["interval_minutes"] = int(cleaned["interval_minutes"])
            if "aht" in cleaned and isinstance(cleaned["aht"], str):
                cleaned["aht"] = float(cleaned["aht"])
            if "historical_volumes" in cleaned:
                if isinstance(cleaned["historical_volumes"], str):
                    cleaned["historical_volumes"] = [
                        float(x.strip())
                        for x in cleaned["historical_volumes"].split(",")
                        if x.strip()
                    ]
                elif isinstance(cleaned["historical_volumes"], list):
                    cleaned["historical_volumes"] = [
                        float(v) for v in cleaned["historical_volumes"]
                    ]

            return VolumeForecastPayload.model_validate(cleaned)
        except ValidationError as exc:
            raise NormalizationError(
                f"Forecast data validation failed: {exc.errors()}", errors=exc.errors()
            ) from exc

    @classmethod
    async def clean_kpi_data(cls, raw: Dict[str, Any]) -> KPIHealthPayload:
        """Map, coerce and validate a raw dict into a KPIHealthPayload."""
        try:
            fields = set(KPIHealthPayload.model_fields.keys())
            cleaned = cls._normalize_keys(raw, cls.KPI_KEY_MAP, fields)

            # Ensure client_id is a string
            if "client_id" in cleaned and not isinstance(cleaned["client_id"], str):
                cleaned["client_id"] = str(cleaned["client_id"])
            # Coerce numeric strings
            for numeric_field in (
                "csat",
                "sla_percent",
                "fcr_percent",
                "rolling_window_days",
            ):
                if numeric_field in cleaned and isinstance(
                    cleaned[numeric_field], str
                ):
                    if numeric_field == "rolling_window_days":
                        cleaned[numeric_field] = int(cleaned[numeric_field])
                    else:
                        cleaned[numeric_field] = float(cleaned[numeric_field])

            return KPIHealthPayload.model_validate(cleaned)
        except ValidationError as exc:
            raise NormalizationError(
                f"KPI data validation failed: {exc.errors()}", errors=exc.errors()
            ) from exc

    @classmethod
    async def clean_adherence_data(
        cls, raw: Dict[str, Any]
    ) -> AdherenceEventPayload:
        """Map, coerce and validate a raw dict into an AdherenceEventPayload."""
        try:
            fields = set(AdherenceEventPayload.model_fields.keys())
            cleaned = cls._normalize_keys(raw, cls.ADHERENCE_KEY_MAP, fields)

            if "agent_id" in cleaned and not isinstance(cleaned["agent_id"], str):
                cleaned["agent_id"] = str(cleaned["agent_id"])
            if "state_slug" in cleaned and not isinstance(
                cleaned["state_slug"], str
            ):
                cleaned["state_slug"] = str(cleaned["state_slug"])
            if "duration_seconds" in cleaned and isinstance(
                cleaned["duration_seconds"], str
            ):
                cleaned["duration_seconds"] = int(cleaned["duration_seconds"])

            return AdherenceEventPayload.model_validate(cleaned)
        except ValidationError as exc:
            raise NormalizationError(
                f"Adherence data validation failed: {exc.errors()}",
                errors=exc.errors(),
            ) from exc