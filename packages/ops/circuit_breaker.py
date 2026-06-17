from __future__ import annotations

import logging
import os

from sqlalchemy.orm import Session, sessionmaker

from packages.core.contracts import DegradationNotice, ErrorCode, ProviderError
from packages.ops.provider_usage_metrics import (
    ProviderProfileHealthMetrics,
    sqlalchemy_provider_profile_health_metrics,
)


logger = logging.getLogger(__name__)

DEFAULT_ERROR_RATE_THRESHOLD = 0.5
DEFAULT_WINDOW_HOURS = 24


class ProviderCircuitBreaker:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def evaluate(self, *, call: object, invocation: object) -> ProviderError | None:
        if os.getenv("CUTAGENT_PROVIDER_CIRCUIT_BREAKER") != "1":
            return None
        provider_profile_id = str(
            getattr(invocation, "provider_profile_id", None)
            or getattr(call, "provider_profile_id", "")
        )
        if not provider_profile_id:
            return None
        threshold = _float_env("CUTAGENT_PROVIDER_CIRCUIT_ERROR_RATE", DEFAULT_ERROR_RATE_THRESHOLD)
        window_hours = _int_env("CUTAGENT_PROVIDER_CIRCUIT_WINDOW", DEFAULT_WINDOW_HOURS)
        metrics = sqlalchemy_provider_profile_health_metrics(
            self.session_factory,
            window_hours=window_hours,
            error_rate_threshold=threshold,
            provider_profile_id=provider_profile_id,
        )
        if not metrics:
            return None
        health = metrics[0]
        if not health.circuit_open:
            return None
        notice = _degradation_notice(health, threshold=threshold)
        logger.warning(
            "provider call blocked by circuit breaker",
            extra={
                "event": "provider.circuit_open",
                "degradation_level": "hard_block",
                "degradation": notice.model_dump(mode="json", warnings=False),
            },
        )
        return ProviderError(
            code=ErrorCode.provider_circuit_open,
            message=(
                f"Provider profile {provider_profile_id} circuit open: "
                f"error_rate={health.error_rate:.3f} "
                f"threshold={threshold:.3f} over {health.window_hours}h."
            ),
            retryable=False,
        )


def _degradation_notice(
    health: ProviderProfileHealthMetrics,
    *,
    threshold: float,
) -> DegradationNotice:
    return DegradationNotice.model_construct(
        code="provider.circuit_open",
        message=(
            f"Provider profile {health.provider_profile_id} circuit open; "
            "provider call failed fast."
        ),
        affects_true_yield=True,
        details={
            "provider_profile_id": health.provider_profile_id,
            "provider_id": health.provider_id,
            "capability_id": health.capability_id,
            "model_id": health.model_id,
            "calls": health.calls,
            "success_count": health.success_count,
            "failure_count": health.failure_count,
            "timeout_or_throttle_count": health.timeout_or_throttle_count,
            "error_rate": health.error_rate,
            "timeout_or_throttle_rate": health.timeout_or_throttle_rate,
            "p95_latency_ms": health.p95_latency_ms,
            "error_rate_threshold": threshold,
            "window_hours": health.window_hours,
        },
    )


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, ""))
    except ValueError:
        return default
    return min(max(value, 0.0), 1.0)


def _int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, ""))
    except ValueError:
        return default
    return max(1, value)
