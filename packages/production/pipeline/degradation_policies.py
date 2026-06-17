from __future__ import annotations

from dataclasses import dataclass


_LIPSYNC_CONTENT_POLICY_MARKERS = (
    "input data may contain inappropriate content",
    "inappropriate content",
    "content policy",
    "sensitive content",
    "unsafe content",
)


@dataclass(frozen=True)
class DegradationPolicy:
    id: str
    version: str


class LipsyncFailoverPolicy(DegradationPolicy):
    def target_provider_id(
        self,
        current_provider_id: str | None,
        error_message: str | None,
    ) -> str | None:
        if current_provider_id == "runninghub.heygem":
            return "dashscope.videoretalk"
        if (
            current_provider_id == "dashscope.videoretalk"
            and _is_lipsync_content_policy_error(error_message)
        ):
            return "runninghub.heygem"
        return None


def _is_lipsync_content_policy_error(message: str | None) -> bool:
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(marker in text for marker in _LIPSYNC_CONTENT_POLICY_MARKERS)


LIPSYNC_FAILOVER_POLICY = LipsyncFailoverPolicy(
    id="lipsync.failover.v1",
    version="v1",
)
ASR_ESTIMATED_FALLBACK_POLICY = DegradationPolicy(
    id="asr.estimated_fallback.v1",
    version="v1",
)
COVER_FALLBACK_POLICY = DegradationPolicy(
    id="cover.fallback.v1",
    version="v1",
)

DEGRADATION_POLICIES = (
    LIPSYNC_FAILOVER_POLICY,
    ASR_ESTIMATED_FALLBACK_POLICY,
    COVER_FALLBACK_POLICY,
)
DEGRADATION_POLICIES_BY_ID = {
    policy.id: policy
    for policy in DEGRADATION_POLICIES
}
