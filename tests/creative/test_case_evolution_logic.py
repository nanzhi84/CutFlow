"""Unit tests for performance scoring, feature extraction, and metrics import."""

from __future__ import annotations

from packages.core.contracts import (
    PerformanceObservation,
    ScriptVersion,
    VideoVersion,
)
from packages.creative.cases import evolution, metrics_import


def _obs(**kwargs) -> PerformanceObservation:
    base = dict(
        id="perf_1",
        case_id="case_x",
        publish_record_id="pr_1",
        metric_name="views",
        metric_value=0.0,
    )
    base.update(kwargs)
    return PerformanceObservation(**base)


# --------------------------------------------------------------------------- #
# §25.6 PerformanceScore
# --------------------------------------------------------------------------- #

def test_score_low_impressions_is_not_high_confidence():
    obs = _obs(id="o1", impressions=50, completion_rate=0.9, window="7d")
    score = evolution.compute_performance_score(obs)
    assert score.excluded_reason == "low_impressions"
    assert score.confidence <= 0.3


def test_score_24h_is_early_signal_only():
    obs = _obs(id="o2", impressions=20000, completion_rate=0.7, window="24h")
    score = evolution.compute_performance_score(obs)
    assert score.excluded_reason == "early_signal_window"


def test_score_mature_window_with_volume_is_active_eligible():
    obs = _obs(id="o3", impressions=20000, completion_rate=0.7, window="7d")
    score = evolution.compute_performance_score(obs)
    assert score.excluded_reason is None
    assert score.primary_metric == "completion_rate"
    assert score.confidence >= 0.6


def test_score_without_normalized_metric_is_excluded():
    obs = _obs(id="o4", impressions=20000, window="7d", metric_name="views", metric_value=20000)
    score = evolution.compute_performance_score(obs)
    assert score.excluded_reason == "no_normalized_metric"
    assert score.normalized_score == 0.0


# --------------------------------------------------------------------------- #
# §25.5 Feature extraction
# --------------------------------------------------------------------------- #

def test_script_feature_extraction_detects_hook_and_cta():
    script = ScriptVersion(
        id="sv1",
        case_id="case_x",
        title="三个理由",
        script="你还在为减肥烦恼吗？\n看看这个方法。\n关注我了解更多。",
    )
    vector = evolution.extract_script_features(script, case_id="case_x", feature_id="cfv1")
    assert vector.hook_type == "question"
    assert vector.cta_type == "follow"
    assert vector.script_structure == "multi_beat"
    assert vector.script_version_id == "sv1"


def test_video_feature_extraction_completes_from_timeline():
    script = ScriptVersion(id="sv2", case_id="case_x", title="t", script="痛点开场。方案。行动。")
    partial = evolution.extract_script_features(script, case_id="case_x", feature_id="cfv2")
    video = VideoVersion(
        id="vv2",
        case_id="case_x",
        script_version_id="sv2",
        timeline_plan_artifact_id="tp1",
        style_plan_artifact_id="sp1",
    )
    timeline = {
        "segments": [
            {"kind": "broll", "duration_sec": 2.0, "material_id": "m1"},
            {"kind": "talking", "duration_sec": 3.0},
        ]
    }
    style = {"bgm_id": "bgm1", "subtitle_style_id": "sub1"}
    vector = evolution.extract_video_features(
        video, feature_id="cfv2", partial=partial, timeline_plan=timeline, style_plan=style
    )
    assert vector.video_version_id == "vv2"
    assert vector.duration_sec == 5.0
    assert vector.broll_count == 1
    assert vector.material_ids == ["m1"]
    assert vector.bgm_id == "bgm1"
    # The partial's script-side features are retained.
    assert vector.hook_type == partial.hook_type


# --------------------------------------------------------------------------- #
# §25.4 matching policy
# --------------------------------------------------------------------------- #

def _record(**kwargs) -> metrics_import.PublishRecordIndex:
    base = dict(publish_record_id="pr_1", video_version_id="vv_1", platform="douyin")
    base.update(kwargs)
    return metrics_import.PublishRecordIndex(**base)


def test_match_external_post_id_policy():
    records = [_record(external_post_id="ext_1")]
    rows = [{"external_post_id": "ext_1", "metric_name": "views", "metric_value": 100}]
    result = metrics_import.match_metrics_rows(rows, policy="external_post_id", records=records)
    assert len(result.matched) == 1
    assert result.matched[0].publish_record_id == "pr_1"
    assert result.matched[0].video_version_id == "vv_1"
    assert not result.unmatched


def test_match_external_ref_falls_back_to_internal_id():
    records = [_record()]
    rows = [{"external_ref": "pr_1", "metric_name": "views", "metric_value": 100}]
    result = metrics_import.match_metrics_rows(rows, policy="external_post_id", records=records)
    assert len(result.matched) == 1
    assert result.matched[0].publish_record_id == "pr_1"


def test_unmatched_rows_are_reported_not_guessed():
    records = [_record(external_post_id="ext_1")]
    rows = [
        {"title": "guess me", "published_at": "2026-01-01", "metric_name": "views", "metric_value": 1},
    ]
    result = metrics_import.match_metrics_rows(rows, policy="external_post_id", records=records)
    assert not result.matched
    assert len(result.unmatched) == 1
    assert result.unmatched[0].reason == "no_deterministic_match"


def test_strict_manual_requires_publish_record_id_and_warns():
    records = [_record()]
    rows = [
        {"publish_record_id": "pr_1", "metric_name": "views", "metric_value": 5},
        {"title": "no id", "metric_name": "views", "metric_value": 5},
    ]
    result = metrics_import.match_metrics_rows(rows, policy="strict_manual", records=records)
    assert len(result.matched) == 1
    assert result.unmatched[0].reason == "publish_record_id_required"
    assert result.warnings  # §25.4: strict_manual writes a warning.


def test_canonical_metrics_are_captured():
    records = [_record(external_post_id="ext_1")]
    rows = [
        {
            "external_post_id": "ext_1",
            "impressions": 10000,
            "views": 8000,
            "completion_rate": 0.55,
            "metric_name": "completion_rate",
            "metric_value": 0.55,
            "window": "7d",
        }
    ]
    result = metrics_import.match_metrics_rows(rows, policy="external_post_id", records=records)
    matched = result.matched[0]
    assert matched.canonical_metrics["impressions"] == 10000
    assert matched.canonical_metrics["completion_rate"] == 0.55
    assert matched.window == "7d"


# --------------------------------------------------------------------------- #
# Regression: scoring must not round-trip an unflushed ORM row (DB-path blocker)
# --------------------------------------------------------------------------- #

def _matched(**kwargs) -> "metrics_import.MatchedRow":
    base = dict(
        row_index=0,
        publish_record_id="pr_1",
        video_version_id="vv_1",
        platform="douyin",
        account_id="acc_1",
        metric_name="completion_rate",
        metric_value=0.42,
        canonical_metrics={"impressions": 50000, "views": 12000, "completion_rate": 0.42},
        window="7d",
    )
    base.update(kwargs)
    return metrics_import.MatchedRow(**base)


def test_observation_contract_from_match_populates_entity_meta_defaults():
    """The canonical builder must yield a contract with non-None EntityMeta fields.

    Regression for the DB-path blocker: the production import scored a contract
    obtained from an *unflushed* ORM row whose created_at/updated_at/schema_version
    were all None, raising a pydantic ValidationError on every matched row.
    """
    obs = metrics_import.observation_contract_from_match("case_x", _matched())
    assert obs.id.startswith("perf_")
    assert obs.created_at is not None
    assert obs.updated_at is not None
    assert obs.schema_version == "v1"
    # canonical metrics fan out onto typed columns
    assert obs.impressions == 50000
    assert obs.views == 12000
    assert obs.window == "7d"
    # the contract scores cleanly on the DB path's exact code path
    score = evolution.compute_performance_score(obs)
    assert score.observation_id == obs.id
    assert score.excluded_reason is None


def test_performance_observation_mappers_round_trip_without_flush_error():
    """Observation row/contract mappers must not require a flush.

    Builds the ORM row from the contract (timestamps already set), then maps it
    back to a contract — exercising the exact pair of calls the production import
    makes, proving no None-timestamp ValidationError can occur on the happy path.
    """
    from packages.core.storage.performance_mappers import (
        performance_observation_row_to_contract,
        performance_observation_to_row,
    )

    obs = metrics_import.observation_contract_from_match("case_x", _matched())
    row = performance_observation_to_row(obs)
    assert row.id == obs.id
    assert row.observed_at is not None
    # scoring is done on the contract, never on the (unflushed) row
    score = evolution.compute_performance_score(obs)
    assert score.normalized_score == 0.42
    # once persisted columns are set (we copy them from the contract), the mapper
    # round-trips; simulate post-flush state by stamping the timestamp columns.
    row.created_at = obs.created_at
    row.updated_at = obs.updated_at
    row.schema_version = obs.schema_version
    mapped = performance_observation_row_to_contract(row)
    assert mapped.id == obs.id
    assert mapped.impressions == 50000
