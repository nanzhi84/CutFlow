from __future__ import annotations

from packages.core.contracts import PerformanceObservation, PerformanceScore
from packages.core.storage.database import PerformanceObservationRow, PerformanceScoreRow


def performance_observation_row_to_contract(
    row: PerformanceObservationRow,
) -> PerformanceObservation:
    return PerformanceObservation(
        id=row.id,
        case_id=row.case_id,
        publish_record_id=row.publish_record_id,
        video_version_id=row.video_version_id,
        platform=row.platform,
        account_id=row.account_id,
        window=row.window,
        metric_name=row.metric_name,
        metric_value=row.metric_value,
        impressions=row.impressions,
        views=row.views,
        avg_watch_sec=row.avg_watch_sec,
        completion_rate=row.completion_rate,
        like_rate=row.like_rate,
        comment_rate=row.comment_rate,
        share_rate=row.share_rate,
        follow_rate=row.follow_rate,
        conversion_count=row.conversion_count,
        conversion_rate=row.conversion_rate,
        raw_metrics=dict(row.raw_metrics or {}),
        observed_at=row.observed_at,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def performance_observation_to_row(
    observation: PerformanceObservation,
) -> PerformanceObservationRow:
    return PerformanceObservationRow(
        id=observation.id,
        case_id=observation.case_id,
        publish_record_id=observation.publish_record_id,
        video_version_id=observation.video_version_id,
        platform=observation.platform,
        account_id=observation.account_id,
        window=observation.window,
        metric_name=observation.metric_name,
        metric_value=observation.metric_value,
        impressions=observation.impressions,
        views=observation.views,
        avg_watch_sec=observation.avg_watch_sec,
        completion_rate=observation.completion_rate,
        like_rate=observation.like_rate,
        comment_rate=observation.comment_rate,
        share_rate=observation.share_rate,
        follow_rate=observation.follow_rate,
        conversion_count=observation.conversion_count,
        conversion_rate=observation.conversion_rate,
        raw_metrics=dict(observation.raw_metrics or {}),
        observed_at=observation.observed_at,
    )


def performance_score_row_to_contract(row: PerformanceScoreRow) -> PerformanceScore:
    return PerformanceScore(
        id=row.id,
        observation_id=row.observation_id,
        case_id=row.case_id,
        video_version_id=row.video_version_id,
        platform=row.platform,
        account_id=row.account_id,
        window=row.window,
        primary_metric=row.primary_metric,
        normalized_score=row.normalized_score,
        confidence=row.confidence,
        sample_size=row.sample_size,
        excluded_reason=row.excluded_reason,
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def performance_score_to_row(score: PerformanceScore) -> PerformanceScoreRow:
    return PerformanceScoreRow(
        id=score.id,
        observation_id=score.observation_id,
        case_id=score.case_id,
        video_version_id=score.video_version_id,
        platform=score.platform,
        account_id=score.account_id,
        window=score.window,
        primary_metric=score.primary_metric,
        normalized_score=score.normalized_score,
        confidence=score.confidence,
        sample_size=score.sample_size,
        excluded_reason=score.excluded_reason,
    )
