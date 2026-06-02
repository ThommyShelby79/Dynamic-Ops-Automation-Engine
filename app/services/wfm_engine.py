import math
from typing import Any, Dict, List

from app.core.models.contracts import VolumeForecastPayload
from app.core.models.tenant import ErlangThresholds

class WFMEngine:
    """
    Asynchronous WFM engine that computes required staffing levels
    using a mathematically accurate Erlang C model.
    """

    @staticmethod
    def _erlang_b(N: int, A: float) -> float:
        """Compute the Erlang B blocking probability."""
        if A == 0:
            return 0.0
        B = 1.0
        for i in range(1, N + 1):
            B = (A * B) / (i + A * B)
        return B

    @classmethod
    def _erlang_c(cls, N: int, A: float) -> float:
        """Compute the Erlang C probability of delay (P_wait)."""
        if N <= A:
            return 1.0
        B = cls._erlang_b(N, A)
        P_wait = B / (1.0 - (A / N) * (1.0 - B))
        return min(P_wait, 1.0)

    @classmethod
    def _required_agents(cls, A: float, target_sl_frac: float, max_agents: int = 10000) -> int:
        """Find the minimum number of agents N to meet the target SL."""
        N = math.ceil(A)
        if N == 0:
            return 1
            
        while N <= max_agents:
            P_wait = cls._erlang_c(N, A)
            service_level = 1.0 - P_wait
            if service_level >= target_sl_frac:
                return N
            N += 1
        return max_agents

    @classmethod
    async def calculate_staffing(
        cls,
        payload: VolumeForecastPayload,
        tenant_config: ErlangThresholds,
    ) -> Dict[str, Any]:
        """Compute required FTE and predicted service levels."""
        interval_min = payload.interval_minutes
        aht_sec = tenant_config.average_call_duration_seconds
        target_sl_frac = tenant_config.target_service_level_percent / 100.0

        # Traffic intensity factor
        factor = aht_sec / (interval_min * 60.0)

        results: List[Dict[str, Any]] = []
        for vol in payload.historical_volumes:
            if vol <= 0:
                agents = 0
                service_level = 100.0
            else:
                A = vol * factor
                agents = cls._required_agents(A, target_sl_frac)
                P_wait = cls._erlang_c(agents, A)
                service_level = (1.0 - P_wait) * 100.0
                
            results.append({
                "volume": vol,
                "agents_required": agents,
                "predicted_service_level": round(service_level, 2),
            })

        return {
            "interval_minutes": interval_min,
            "aht_seconds": aht_sec,
            "target_service_level": tenant_config.target_service_level_percent,
            "predictions": results,
        }