from __future__ import annotations

from datetime import datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from packages.core.contracts import NodeRun, ProviderInvocation, ProviderStatus, WorkflowRun
from packages.core.storage import Repository


REQUIRED_LOG_FIELDS = (
    "request_id",
    "trace_id",
    "user_id",
    "case_id",
    "job_id",
    "run_id",
    "node_run_id",
    "provider_invocation_id",
    "prompt_invocation_id",
)

REGISTRY = CollectorRegistry()

API_REQUEST_DURATION = Histogram(
    "api_request_duration_seconds",
    "API request duration.",
    registry=REGISTRY,
)
API_REQUEST_ERRORS = Counter(
    "api_request_errors_total",
    "API request errors.",
    registry=REGISTRY,
)
WORKFLOW_RUN_DURATION = Histogram(
    "workflow_run_duration_seconds",
    "Workflow run duration.",
    registry=REGISTRY,
)
NODE_RUN_DURATION = Histogram(
    "node_run_duration_seconds",
    "Node run duration.",
    registry=REGISTRY,
)
NODE_RUN_RETRIES = Counter(
    "node_run_retries_total",
    "Node run retries.",
    registry=REGISTRY,
)
PROVIDER_INVOCATION_DURATION = Histogram(
    "provider_invocation_duration_seconds",
    "Provider invocation duration.",
    registry=REGISTRY,
)
PROVIDER_INVOCATION_FAILURES = Counter(
    "provider_invocation_failures_total",
    "Provider invocation failures.",
    registry=REGISTRY,
)
PROVIDER_COST_ESTIMATED = Counter(
    "provider_cost_estimated_total",
    "Estimated provider cost.",
    registry=REGISTRY,
)
PROVIDER_UNPRICED_INVOCATIONS = Counter(
    "provider_unpriced_invocations_total",
    "Unpriced provider invocations.",
    registry=REGISTRY,
)
PROVIDER_CALL_REPLAYS = Counter(
    "provider_call_replays_total",
    "Provider calls answered from a durable result instead of the vendor.",
    registry=REGISTRY,
)
PROVIDER_CALL_REOPENS = Counter(
    "provider_call_reopens_total",
    "Provider calls re-submitted (and re-billed) because the durable result was unusable.",
    ["reason"],
    registry=REGISTRY,
)
YIELD_FUNNEL_EVENTS = Counter(
    "yield_funnel_events_total",
    "Yield funnel events.",
    registry=REGISTRY,
)
OUTBOX_LAG = Gauge(
    "outbox_lag_seconds",
    "Oldest pending outbox event lag.",
    registry=REGISTRY,
)
TEMPORAL_ACTIVITY_FAILURES = Counter(
    "temporal_activity_failures_total",
    "Temporal activity failures.",
    registry=REGISTRY,
)
# Cross-process Redis coordination health (issue #67). ``component`` is one of
# event_fanout / event_token_store / provider_limiter. The gauge is 1 while that
# layer has degraded to its per-process fallback, 0 once it reconnects to Redis.
REDIS_DEGRADED = Gauge(
    "redis_degraded",
    "Redis coordination layer degraded to per-process fallback (1=degraded).",
    ["component"],
    registry=REGISTRY,
)
REDIS_RECONNECT_ATTEMPTS = Counter(
    "redis_reconnect_attempts_total",
    "Attempts to reconnect a degraded Redis coordination layer.",
    ["component"],
    registry=REGISTRY,
)
# Run event-stream (WebSocket) health (issue #74). Heartbeats keep idle proxy
# connections from being closed; the gauge/counters expose connection churn.
EVENT_STREAM_CONNECTIONS_ACTIVE = Gauge(
    "event_stream_connections_active",
    "Currently open run event-stream WebSocket connections.",
    registry=REGISTRY,
)
EVENT_STREAM_DISCONNECTS = Counter(
    "event_stream_disconnects_total",
    "Run event-stream WebSocket disconnects.",
    registry=REGISTRY,
)
EVENT_STREAM_HEARTBEATS_SENT = Counter(
    "event_stream_heartbeats_sent_total",
    "Server-side heartbeats sent on idle run event-stream connections.",
    registry=REGISTRY,
)


def record_redis_degraded(component: str) -> None:
    REDIS_DEGRADED.labels(component=component).set(1)


def record_redis_recovered(component: str) -> None:
    REDIS_DEGRADED.labels(component=component).set(0)


def record_redis_reconnect_attempt(component: str) -> None:
    REDIS_RECONNECT_ATTEMPTS.labels(component=component).inc()


def record_event_stream_connected() -> None:
    EVENT_STREAM_CONNECTIONS_ACTIVE.inc()


def record_event_stream_disconnected() -> None:
    EVENT_STREAM_CONNECTIONS_ACTIVE.dec()
    EVENT_STREAM_DISCONNECTS.inc()


def record_event_stream_heartbeat() -> None:
    EVENT_STREAM_HEARTBEATS_SENT.inc()


def record_api_request(duration_seconds: float, status_code: int) -> None:
    API_REQUEST_DURATION.observe(duration_seconds)
    if status_code >= 500:
        API_REQUEST_ERRORS.inc()


def record_workflow_run(run: WorkflowRun) -> None:
    if run.started_at is None or run.finished_at is None:
        return
    WORKFLOW_RUN_DURATION.observe(_duration_seconds(run.started_at, run.finished_at))


def record_node_run(node_run: NodeRun) -> None:
    if node_run.started_at is not None and node_run.finished_at is not None:
        NODE_RUN_DURATION.observe(_duration_seconds(node_run.started_at, node_run.finished_at))
    if node_run.attempt > 1:
        NODE_RUN_RETRIES.inc(node_run.attempt - 1)


def record_provider_invocation(invocation: ProviderInvocation) -> None:
    if invocation.duration_ms is not None:
        PROVIDER_INVOCATION_DURATION.observe(max(0, invocation.duration_ms) / 1000)
    if invocation.status in {ProviderStatus.failed, ProviderStatus.timed_out}:
        PROVIDER_INVOCATION_FAILURES.inc()
    if invocation.estimated_cost is not None:
        PROVIDER_COST_ESTIMATED.inc(float(invocation.estimated_cost.amount))
    if invocation.billing_status == "unpriced":
        PROVIDER_UNPRICED_INVOCATIONS.inc()


def record_provider_call_replayed() -> None:
    # Deliberately NOT record_provider_invocation: a replay costs nothing, so counting
    # its duration/cost again would double the spend the reports show.
    PROVIDER_CALL_REPLAYS.inc()


def record_provider_call_reopened(reason: str) -> None:
    PROVIDER_CALL_REOPENS.labels(reason=reason).inc()


def record_yield_funnel_event() -> None:
    YIELD_FUNNEL_EVENTS.inc()


def record_temporal_activity_failure() -> None:
    TEMPORAL_ACTIVITY_FAILURES.inc()


def update_outbox_lag(repository: Repository) -> None:
    pending = [event for event in repository.outbox.values() if event.status == "pending"]
    if not pending:
        OUTBOX_LAG.set(0)
        return
    oldest = min(event.created_at for event in pending)
    OUTBOX_LAG.set(max(0, _duration_seconds(oldest, datetime.now(oldest.tzinfo))))


def metric_snapshot(repository: Repository) -> str:
    update_outbox_lag(repository)
    return generate_latest(REGISTRY).decode("utf-8")


def _duration_seconds(start: datetime, end: datetime) -> float:
    return max(0, (end - start).total_seconds())
