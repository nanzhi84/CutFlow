from __future__ import annotations

from packages.production.pipeline.degradation_policies import (
    ASR_ESTIMATED_FALLBACK_POLICY,
    COVER_FALLBACK_POLICY,
    DEGRADATION_POLICIES_BY_ID,
    LIPSYNC_FAILOVER_POLICY,
)


def test_degradation_policy_ids_and_versions_are_stable():
    assert LIPSYNC_FAILOVER_POLICY.id == "lipsync.failover.v1"
    assert LIPSYNC_FAILOVER_POLICY.version == "v1"
    assert ASR_ESTIMATED_FALLBACK_POLICY.id == "asr.estimated_fallback.v1"
    assert ASR_ESTIMATED_FALLBACK_POLICY.version == "v1"
    assert COVER_FALLBACK_POLICY.id == "cover.fallback.v1"
    assert COVER_FALLBACK_POLICY.version == "v1"
    assert DEGRADATION_POLICIES_BY_ID == {
        policy.id: policy
        for policy in (
            LIPSYNC_FAILOVER_POLICY,
            ASR_ESTIMATED_FALLBACK_POLICY,
            COVER_FALLBACK_POLICY,
        )
    }
