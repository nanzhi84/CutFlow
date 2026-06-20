"""§9.5 / §26.3 成品率指标 computation off ``yield_funnel_events``.

The §9.5 funnel persists one row per lifecycle milestone (see
``packages.core.observability.funnel``). This module derives the 11 spec-mandated
yield rates from those rows with the EXACT §26.3 denominators:

* ``technical_success_rate`` = succeeded runs / started runs.
* ``finished_video_rate``    = jobs with a finished video / submitted jobs.
* ``qc_pass_rate``           = qc_passed finished videos / finished videos checked.
* ``approval_pass_rate``     = manual_approved / manual reviews started.
* ``publish_success_rate``   = published packages / publish_started packages.
* ``true_yield_rate``        = business-usable finished videos / submitted JOBS.
* ``rework_rate``            = reworked finished videos / finished videos.
* ``discard_rate``           = discarded finished videos / finished videos.
* ``stage_pass_rate``        = node_succeeded / node_started (§26.3 denom = node_started).
* ``provider_success_rate``  = passed through from provider_usage_metrics.
* ``prompt_version_yield``   = per prompt_version, true-yield runs / runs that used it.

去重 (§26.3): retry/resume multiple runs belong to the SAME job; true_yield and
finished_video denominators use the JOB, not the run. Each rate is ``None`` when
its denominator is 0 (no data), never a misleading 0.0.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from packages.core.contracts import YieldRates
from packages.core.observability.funnel import (
    TRUE_YIELD_DISQUALIFIERS,
    TRUE_YIELD_SUCCESS,
)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return numerator / denominator


# Funnel event_types that mark a finished video as reworked / discarded. These are
# emitted by the QC / lifecycle layer when a finished video is sent back or dropped.
_REWORK_EVENTS = frozenset({"rework_required", "reworked"})
_DISCARD_EVENTS = frozenset({"discarded", "discard"})


def compute_yield_rates(
    events: Iterable[object],
    *,
    provider_success_rate: float | None = None,
    run_prompt_versions: Mapping[str, Iterable[str]] | None = None,
) -> YieldRates:
    """Compute the §26.3 yield rate set from §9.5 funnel events.

    ``events`` is any iterable of objects exposing ``event_type`` / ``run_id`` /
    ``job_id`` / ``finished_video_id`` attributes (the ``YieldFunnelEvent``
    contract or its ORM/in-memory equivalents). ``provider_success_rate`` is
    passed through unchanged (it is produced server-side by
    ``provider_usage_metrics``). ``run_prompt_versions`` maps a run_id to the
    prompt_version_ids it invoked, enabling ``prompt_version_yield``.
    """

    started_runs: set[str] = set()
    succeeded_runs: set[str] = set()  # runs that produced node_succeeded (technical success)
    failed_runs: set[str] = set()
    published_runs: set[str] = set()
    disqualified_runs: set[str] = set()

    submitted_jobs: set[str] = set()
    finished_video_jobs: set[str] = set()

    finished_videos: set[str] = set()
    qc_checked_videos: set[str] = set()
    qc_passed_videos: set[str] = set()
    reworked_videos: set[str] = set()
    discarded_videos: set[str] = set()

    manual_started = 0
    manual_approved = 0

    publish_started_packages: set[str] = set()
    published_packages: set[str] = set()

    node_started = 0
    node_succeeded = 0

    for event in events:
        event_type = getattr(event, "event_type", None)
        run_id = getattr(event, "run_id", None)
        job_id = getattr(event, "job_id", None)
        fv_id = getattr(event, "finished_video_id", None)
        package_id = getattr(event, "publish_package_id", None)

        if run_id:
            if event_type in ("submitted", "admitted", "started"):
                started_runs.add(run_id)
            if event_type == "node_succeeded":
                succeeded_runs.add(run_id)
            if event_type == "node_failed":
                failed_runs.add(run_id)
            if event_type == TRUE_YIELD_SUCCESS:
                published_runs.add(run_id)
            if event_type in TRUE_YIELD_DISQUALIFIERS:
                disqualified_runs.add(run_id)

        if job_id:
            submitted_jobs.add(job_id)
            if event_type == "finished_video_created":
                finished_video_jobs.add(job_id)

        if event_type == "finished_video_created" and fv_id:
            finished_videos.add(fv_id)

        if event_type in ("qc_passed", "qc_failed"):
            target = fv_id or run_id
            if target:
                qc_checked_videos.add(target)
                if event_type == "qc_passed":
                    qc_passed_videos.add(target)

        if event_type in _REWORK_EVENTS and (fv_id or run_id):
            reworked_videos.add(fv_id or run_id)
        if event_type in _DISCARD_EVENTS and (fv_id or run_id):
            discarded_videos.add(fv_id or run_id)

        if event_type in ("manual_approved", "manual_rejected"):
            manual_started += 1
            if event_type == "manual_approved":
                manual_approved += 1

        if event_type == "publish_started":
            publish_started_packages.add(package_id or run_id or "")
        if event_type == TRUE_YIELD_SUCCESS:
            published_packages.add(package_id or run_id or "")

        if event_type == "node_started":
            node_started += 1
        if event_type == "node_succeeded":
            node_succeeded += 1

    # finished-video denominator: prefer explicit finished_video ids, fall back to
    # the count of jobs that produced a finished video (handles funnel rows that
    # carry job_id but no finished_video_id).
    finished_video_denom = len(finished_videos) or len(finished_video_jobs)

    true_yield_jobs = _true_yield_jobs(
        published_runs, disqualified_runs, run_to_job=_run_to_job(events)
    )

    prompt_version_yield = _prompt_version_yield(
        run_prompt_versions=run_prompt_versions,
        published_runs=published_runs,
        disqualified_runs=disqualified_runs,
    )

    return YieldRates(
        technical_success_rate=_ratio(len(succeeded_runs - failed_runs), len(started_runs)),
        finished_video_rate=_ratio(len(finished_video_jobs), len(submitted_jobs)),
        qc_pass_rate=_ratio(len(qc_passed_videos), len(qc_checked_videos)),
        approval_pass_rate=_ratio(manual_approved, manual_started),
        publish_success_rate=_ratio(
            len([p for p in published_packages if p]), len([p for p in publish_started_packages if p])
        ),
        true_yield_rate=_ratio(len(true_yield_jobs), len(submitted_jobs)),
        rework_rate=_ratio(len(reworked_videos), finished_video_denom),
        discard_rate=_ratio(len(discarded_videos), finished_video_denom),
        stage_pass_rate=_ratio(node_succeeded, node_started),
        provider_success_rate=provider_success_rate,
        prompt_version_yield=prompt_version_yield,
    )


def _run_to_job(events: Iterable[object]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for event in events:
        run_id = getattr(event, "run_id", None)
        job_id = getattr(event, "job_id", None)
        if run_id and job_id and run_id not in mapping:
            mapping[run_id] = job_id
    return mapping


def _true_yield_jobs(
    published_runs: set[str],
    disqualified_runs: set[str],
    *,
    run_to_job: Mapping[str, str],
) -> set[str]:
    """§26.3: true_yield_rate denominator uses the JOB (retry/resume runs collapse
    to one job). A job is true-yield if ANY of its runs reached published and that
    run was not disqualified."""

    jobs: set[str] = set()
    for run_id in published_runs - disqualified_runs:
        jobs.add(run_to_job.get(run_id, run_id))
    return jobs


def _prompt_version_yield(
    *,
    run_prompt_versions: Mapping[str, Iterable[str]] | None,
    published_runs: set[str],
    disqualified_runs: set[str],
) -> dict[str, float]:
    if not run_prompt_versions:
        return {}
    true_yield_runs = published_runs - disqualified_runs
    totals: dict[str, int] = {}
    wins: dict[str, int] = {}
    for run_id, versions in run_prompt_versions.items():
        for version_id in set(versions):
            if not version_id:
                continue
            totals[version_id] = totals.get(version_id, 0) + 1
            if run_id in true_yield_runs:
                wins[version_id] = wins.get(version_id, 0) + 1
    return {
        version_id: wins.get(version_id, 0) / total
        for version_id, total in totals.items()
        if total > 0
    }
