from __future__ import annotations

from dataclasses import dataclass


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
        # Platform-side LipSync provider failover is disabled. Keep the stable
        # policy object for historical degradation records, but do not choose a
        # second provider after the requested provider fails.
        _ = (current_provider_id, error_message)
        return None


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
