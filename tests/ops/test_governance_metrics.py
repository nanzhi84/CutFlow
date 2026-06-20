"""Unit coverage for the §9.4 / §9.5 / §9.6 / §9.8 governance compute layer (PR6).

These exercise the pure ``packages.ops`` compute functions directly (no DB / API),
asserting the §26.2 cost formulas, §26.3 yield denominators, budget evaluation,
and failure classification.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from packages.core.contracts import Budget, FailureClass, Money
from packages.ops import (
    FunnelCounts,
    InvocationCost,
    SpendRecord,
    classify_error_code,
    compute_cost_metrics,
    compute_yield_rates,
    evaluate_budget,
)
from packages.ops.budget_evaluation import period_start


class _Event:
    def __init__(self, event_type, run_id=None, job_id=None, finished_video_id=None, publish_package_id=None):
        self.event_type = event_type
        self.run_id = run_id
        self.job_id = job_id
        self.finished_video_id = finished_video_id
        self.publish_package_id = publish_package_id


# ---------------- §26.3 yield rates ----------------


def test_yield_rates_use_spec_26_3_denominators():
    events = [
        # job_a / run_a: full success -> finished video, qc passed, published.
        _Event("submitted", run_id="run_a", job_id="job_a"),
        _Event("node_started", run_id="run_a", job_id="job_a"),
        _Event("node_succeeded", run_id="run_a", job_id="job_a"),
        _Event("finished_video_created", run_id="run_a", job_id="job_a", finished_video_id="fv_a"),
        _Event("qc_passed", run_id="run_a", job_id="job_a", finished_video_id="fv_a"),
        _Event("publish_started", run_id="run_a", job_id="job_a", publish_package_id="pkg_a"),
        _Event("published", run_id="run_a", job_id="job_a", publish_package_id="pkg_a"),
        # job_b / run_b: started but node failed (no finished video).
        _Event("submitted", run_id="run_b", job_id="job_b"),
        _Event("node_started", run_id="run_b", job_id="job_b"),
        _Event("node_failed", run_id="run_b", job_id="job_b"),
    ]
    rates = compute_yield_rates(events, provider_success_rate=0.9)
    # 2 submitted jobs; 1 produced a finished video.
    assert rates.finished_video_rate == 0.5
    # 1 qc-passed of 1 checked.
    assert rates.qc_pass_rate == 1.0
    # 1 published of 1 publish_started package.
    assert rates.publish_success_rate == 1.0
    # true_yield denominator is JOBS (2); 1 job true-yield.
    assert rates.true_yield_rate == 0.5
    # stage_pass denominator is node_started (2); 1 node_succeeded.
    assert rates.stage_pass_rate == 0.5
    # provider_success_rate is passed through.
    assert rates.provider_success_rate == 0.9
    # technical_success: started runs 2, succeeded-not-failed runs = run_a only.
    assert rates.technical_success_rate == 0.5


def test_yield_rates_qc_failed_excluded_from_true_yield():
    events = [
        _Event("submitted", run_id="run_a", job_id="job_a"),
        _Event("finished_video_created", run_id="run_a", job_id="job_a", finished_video_id="fv_a"),
        _Event("published", run_id="run_a", job_id="job_a", publish_package_id="pkg_a"),
        _Event("qc_failed", run_id="run_a", job_id="job_a", finished_video_id="fv_a"),
    ]
    rates = compute_yield_rates(events)
    # The only job published but qc_failed -> not true yield.
    assert rates.true_yield_rate == 0.0
    assert rates.qc_pass_rate == 0.0


def test_yield_rates_none_denominators_return_none():
    rates = compute_yield_rates([])
    assert rates.true_yield_rate is None
    assert rates.qc_pass_rate is None
    assert rates.stage_pass_rate is None


def test_prompt_version_yield_is_per_version_true_yield():
    events = [
        _Event("submitted", run_id="run_a", job_id="job_a"),
        _Event("published", run_id="run_a", job_id="job_a"),
        _Event("submitted", run_id="run_b", job_id="job_b"),
        _Event("node_started", run_id="run_b", job_id="job_b"),
    ]
    rates = compute_yield_rates(
        events,
        run_prompt_versions={"run_a": ["pv_1"], "run_b": ["pv_1"]},
    )
    # pv_1 used by 2 runs, 1 of which (run_a) is true yield -> 0.5.
    assert rates.prompt_version_yield["pv_1"] == 0.5


# ---------------- §26.2 cost metrics ----------------


def _inv(amount, **kw):
    defaults = dict(
        estimated_amount=Decimal(str(amount)),
        actual_amount=None,
        currency="CNY",
        provider_id="minimax",
        model_id="speech-01",
        prompt_version_id=None,
        run_id="run_a",
        run_is_failed=False,
        run_is_retry=False,
        node_attempt=1,
    )
    defaults.update(kw)
    return InvocationCost(**defaults)


def test_cost_metrics_unit_costs_and_attribution():
    invocations = [
        _inv("10", provider_id="minimax", model_id="speech-01", prompt_version_id="pv_1"),
        _inv("30", provider_id="dashscope", model_id="video-01", prompt_version_id="pv_2"),
    ]
    counts = FunnelCounts(finished_video_count=2, qc_passed_count=1, published_count=1)
    metrics = compute_cost_metrics(invocations, counts)
    assert metrics.estimated_cost.amount == Decimal("40")
    assert metrics.unit_cost_per_finished_video.amount == Decimal("20")  # 40 / 2
    assert metrics.unit_cost_per_qc_passed_video.amount == Decimal("40")  # 40 / 1
    assert metrics.unit_cost_per_published_video.amount == Decimal("40")  # 40 / 1
    assert metrics.provider_cost["minimax"].amount == Decimal("10")
    assert metrics.provider_cost["dashscope"].amount == Decimal("30")
    assert metrics.model_cost["video-01"].amount == Decimal("30")
    assert metrics.prompt_version_cost["pv_1"].amount == Decimal("10")


def test_cost_metrics_wasted_retry_and_variance():
    invocations = [
        _inv("10", run_id="run_failed", run_is_failed=True),
        _inv("5", run_id="run_retry", run_is_retry=True),
        _inv("7", run_id="run_attempt2", node_attempt=2),
        _inv("3", run_id="run_ok", actual_amount=Decimal("4")),
    ]
    counts = FunnelCounts(finished_video_count=0, qc_passed_count=0, published_count=0)
    metrics = compute_cost_metrics(invocations, counts)
    # wasted = failed run cost (10).
    assert metrics.wasted_cost.amount == Decimal("10")
    # retry = retry run (5) + node attempt > 1 (7) = 12.
    assert metrics.retry_cost.amount == Decimal("12")
    # variance = actual(4) - estimated(25) = -21 (only one inv has actual).
    assert metrics.actual_cost.amount == Decimal("4")
    assert metrics.cost_variance.amount == Decimal("-21")
    # zero finished videos -> unit costs are None (not a misleading 0).
    assert metrics.unit_cost_per_finished_video is None


# ---------------- §9.8 budget evaluation ----------------


def test_budget_evaluation_threshold_and_exceed():
    now = datetime.now(timezone.utc)
    budget = Budget(
        id="b1",
        scope_type="provider",
        scope_id="minimax",
        period="day",
        limit=Money(amount=Decimal("100"), currency="CNY"),
        alert_threshold=0.8,
    )
    records = [
        SpendRecord(amount=Decimal("85"), currency="CNY", created_at=now, provider_id="minimax"),
        # different provider -> excluded from this scope.
        SpendRecord(amount=Decimal("50"), currency="CNY", created_at=now, provider_id="dashscope"),
        # before the period start -> excluded.
        SpendRecord(amount=Decimal("999"), currency="CNY", created_at=now - timedelta(days=2), provider_id="minimax"),
    ]
    evaluation = evaluate_budget(budget, records, now=now)
    assert evaluation.spend.amount == Decimal("85")
    assert evaluation.ratio == 0.85
    assert evaluation.threshold_crossed is True
    assert evaluation.exceeded is False


def test_budget_period_start_resets_daily():
    now = datetime(2026, 6, 15, 13, 30, tzinfo=timezone.utc)
    assert period_start("day", now) == datetime(2026, 6, 15, tzinfo=timezone.utc)
    assert period_start("month", now) == datetime(2026, 6, 1, tzinfo=timezone.utc)


# ---------------- §9.6 failure taxonomy ----------------


def test_failure_classification_maps_15_classes():
    assert classify_error_code("provider.timeout") == FailureClass.provider_timeout
    assert classify_error_code("provider.quota_exceeded") == FailureClass.quota_exceeded
    assert classify_error_code("provider.cost_unpriced") == FailureClass.price_missing
    assert classify_error_code("render.invalid_timeline") == FailureClass.timeline_invalid
    assert classify_error_code("render.subtitle_failed") == FailureClass.subtitle_failed
    assert classify_error_code("publish.failed") == FailureClass.publish_failed
    assert classify_error_code("material.insufficient.portrait") == FailureClass.material_insufficient
    # Prefix fallback + unknown -> provider_error.
    assert classify_error_code("provider.brand_new_code") == FailureClass.provider_error
    assert classify_error_code("totally.unknown") == FailureClass.provider_error
    assert classify_error_code(None) == FailureClass.provider_error
