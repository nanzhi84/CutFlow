"""Unit tests for the Case self-evolution pure logic (Spec §8.4 / §25.4-25.8)."""

from __future__ import annotations

from datetime import timedelta

from packages.core.contracts import (
    CaseMemory,
    CaseMemoryScope,
    CreativeBrief,
    PerformanceObservation,
    ScriptVersion,
    VideoVersion,
    utcnow,
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
    assert not evolution.score_is_active_eligible(score)


def test_score_mature_window_with_volume_is_active_eligible():
    obs = _obs(id="o3", impressions=20000, completion_rate=0.7, window="7d")
    score = evolution.compute_performance_score(obs)
    assert score.excluded_reason is None
    assert score.primary_metric == "completion_rate"
    assert score.confidence >= evolution.MEMORY_ACTIVATION_MIN_CONFIDENCE
    assert evolution.score_is_active_eligible(score)


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
# §25.8 Memory recall
# --------------------------------------------------------------------------- #

def _memory(mid: str, **kwargs) -> CaseMemory:
    scope = kwargs.pop("scope", CaseMemoryScope())
    base = dict(
        id=mid,
        case_id="case_x",
        status="active",
        insight=f"insight {mid}",
        confidence=0.7,
    )
    base.update(kwargs)
    return CaseMemory(scope=scope, **base)


def test_recall_filters_inactive_and_expired():
    now = utcnow()
    active = _memory("m_active")
    inactive = _memory("m_inactive", status="proposed")
    expired = _memory(
        "m_expired",
        scope=CaseMemoryScope(valid_until=now - timedelta(days=1)),
    )
    future = _memory(
        "m_future",
        scope=CaseMemoryScope(valid_from=now + timedelta(days=1)),
    )
    recalled = evolution.filter_recall_memories(
        [active, inactive, expired, future], mode="recent", now=now
    )
    ids = {m.id for m in recalled}
    assert ids == {"m_active"}


def test_recall_platform_mode_filters_by_scope_platform():
    douyin = _memory("m_dy", scope=CaseMemoryScope(applies_to_platforms=["douyin"]))
    other = _memory("m_ks", scope=CaseMemoryScope(applies_to_platforms=["kuaishou"]))
    recalled = evolution.filter_recall_memories(
        [douyin, other], mode="platform", platform="douyin"
    )
    assert [m.id for m in recalled] == ["m_dy"]


def test_recall_memory_type_mode():
    pat = _memory("m_pat", memory_type="script_pattern")
    neg = _memory("m_neg", memory_type="negative_lesson")
    recalled = evolution.filter_recall_memories(
        [pat, neg], mode="memory_type", memory_type="negative_lesson"
    )
    assert [m.id for m in recalled] == ["m_neg"]


def test_recall_high_performance_uses_score_lookup():
    a = _memory("m_a", scope=CaseMemoryScope(scope_key="douyin"), confidence=0.6)
    b = _memory("m_b", scope=CaseMemoryScope(scope_key="kuaishou"), confidence=0.6)
    recalled = evolution.filter_recall_memories(
        [a, b],
        mode="high_performance",
        score_lookup={"douyin": 0.9, "kuaishou": 0.2},
    )
    assert [m.id for m in recalled] == ["m_a", "m_b"]
    recalled_low = evolution.filter_recall_memories(
        [a, b],
        mode="low_performance",
        score_lookup={"douyin": 0.9, "kuaishou": 0.2},
    )
    assert [m.id for m in recalled_low] == ["m_b", "m_a"]


# --------------------------------------------------------------------------- #
# §8.4 analysis + proposals
# --------------------------------------------------------------------------- #

def test_analysis_groups_by_platform_account_window_with_sample_size():
    obs1 = _obs(id="o1", platform="douyin", account_id="acc1", window="7d", completion_rate=0.8, impressions=20000)
    obs2 = _obs(id="o2", platform="douyin", account_id="acc1", window="7d", completion_rate=0.7, impressions=20000)
    obs3 = _obs(id="o3", platform="kuaishou", account_id="acc2", window="7d", completion_rate=0.3, impressions=20000)
    scores = [evolution.compute_performance_score(o) for o in (obs1, obs2, obs3)]
    analysis = evolution.analyze_historical_performance([obs1, obs2, obs3], scores)
    by_platform = {g["platform"]: g for g in analysis}
    assert by_platform["douyin"]["sample_size"] == 2
    assert by_platform["douyin"]["confident_sample_size"] == 2
    assert by_platform["kuaishou"]["sample_size"] == 1


def test_build_proposals_are_data_driven_with_evidence_and_dedup():
    obs_hi = _obs(id="o1", platform="douyin", window="7d", completion_rate=0.85, impressions=20000)
    obs_lo = _obs(id="o2", platform="kuaishou", window="7d", completion_rate=0.1, impressions=20000)
    scores = [evolution.compute_performance_score(o) for o in (obs_hi, obs_lo)]
    analysis = evolution.analyze_historical_performance([obs_hi, obs_lo], scores)
    brief = CreativeBrief(id="b1", case_id="case_x", summary="减肥产品种草", topic="减肥")
    counter = 0

    def factory() -> str:
        nonlocal counter
        counter += 1
        return f"mem_{counter}"

    proposals = evolution.build_memory_proposals(
        case_id="case_x",
        reflection_run_id="refl_1",
        analysis=analysis,
        briefs=[brief],
        id_factory=factory,
    )
    assert proposals, "expected data-driven proposals"
    # Evidence carries the observation ids + the reflection run id.
    for proposal in proposals:
        assert "refl_1" in proposal.evidence
        assert proposal.sample_size >= 1
        assert proposal.scope.scope_key in {"douyin", "kuaishou"}
    types = {p.memory_type for p in proposals}
    assert "negative_lesson" in types  # the low scorer becomes a counter-example

    # Re-running dedups against existing active + proposed.
    again = evolution.build_memory_proposals(
        case_id="case_x",
        reflection_run_id="refl_2",
        analysis=analysis,
        briefs=[brief],
        existing_proposed=proposals,
        id_factory=factory,
    )
    assert again == []


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
