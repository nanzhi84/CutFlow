"""Provider-profile selection for the digital-human pipeline.

Extracted from ``LocalRuntimeAdapter`` so the orchestrator stays a thin engine.
A :class:`ProviderProfileResolver` holds the gating rules that decide, for a
given capability, whether a *real* provider profile is available — enabled, with
its plugin registered, not the seeded ``sandbox`` provider, and (if it carries
one) with an active secret. When no real profile is armed the rules either fall
back to a seeded sandbox profile (when ``sandbox_fallback_allowed()``) or fail
loudly, never silently producing sandbox output in production.

The resolver is stateless beyond the two collaborators it holds (the repository
and the provider gateway), so ``LocalRuntimeAdapter`` exposes it as a cached
property and node handlers reach it through ``NodeContext`` proxies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from packages.core.config.settings import sandbox_fallback_allowed
from packages.core.contracts import DigitalHumanVideoRequest, ErrorCode
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline.degradation_policies import LIPSYNC_FAILOVER_POLICY

if TYPE_CHECKING:  # pragma: no cover - typing only
    from packages.ai.gateway import ProviderGateway
    from packages.core.contracts import ProviderProfile
    from packages.core.storage import Repository


class ProviderProfileResolver:
    """Resolve real-vs-sandbox provider profiles for the pipeline's capabilities.

    Holds the two collaborators the gating rules consult: the ``repository``
    (provider profiles + voice bindings) and the ``provider_gateway`` (which
    plugins are registered and whether a secret is active).
    """

    def __init__(self, repository: "Repository", provider_gateway: "ProviderGateway") -> None:
        self.repository = repository
        self.provider_gateway = provider_gateway

    def first_available(
        self, capability: str, *, include_sandbox: bool = True
    ) -> "ProviderProfile | None":
        for profile in self.repository.provider_profiles.values():
            if profile.capability != capability or not profile.enabled:
                continue
            if not include_sandbox and profile.provider_id == "sandbox":
                continue
            if profile.provider_id not in self.provider_gateway.plugins:
                continue
            if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
                continue
            return profile
        return None

    def tts_profile_id(self, request: DigitalHumanVideoRequest) -> str:
        explicit_profile_id = request.voice.provider_profile_id
        voice = self.repository.voices.get(request.voice.voice_id or "")
        voice_profile_id = voice.provider_profile_id if voice is not None else None
        profile_id = explicit_profile_id or voice_profile_id

        def _fallback_or_raise(reason: str) -> str:
            if sandbox_fallback_allowed():
                return "sandbox.tts.default"
            raise NodeExecutionError(
                ErrorCode.provider_unsupported_option,
                f"未配置可用的真实 TTS 供应商（{reason}）。请在「设置」中配置并启用真实 TTS 供应商及密钥。",
            )

        if not profile_id:
            return _fallback_or_raise("声音未绑定供应商配置")
        profile = self._profile_by_id(profile_id)
        if profile is None or profile.capability != "tts.speech":
            if explicit_profile_id:
                raise NodeExecutionError(
                    ErrorCode.provider_unsupported_option,
                    "TTS provider profile is missing or incompatible.",
                )
            return _fallback_or_raise("声音的供应商配置缺失或能力不匹配")
        if not profile.enabled:
            return _fallback_or_raise(f"供应商配置 {profile.id} 未启用")
        if profile.provider_id not in self.provider_gateway.plugins:
            return _fallback_or_raise(f"供应商 {profile.provider_id} 未注册")
        if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
            return _fallback_or_raise(f"供应商配置 {profile.id} 的密钥未激活")
        return profile.id

    def image_cover_profile_id(self, request: DigitalHumanVideoRequest) -> str | None:
        """Return a real ``image.generate`` ProviderProfile id only when AI cover
        is requested AND an enabled real profile + active secret exist. Otherwise
        ``None`` -> the cover node uses the existing frame-based cover. AI cover is
        PAID, so without a configured+secret-active image profile we never call it."""
        explicit_profile_id = request.cover.template_id
        if explicit_profile_id:
            profile = self._profile_by_id(explicit_profile_id)
            return profile.id if self._is_real_image(profile) else None
        for profile in self.repository.provider_profiles.values():
            if self._is_real_image(profile):
                return profile.id
        return None

    def _is_real_image(self, profile) -> bool:
        if profile is None or profile.capability != "image.generate" or not profile.enabled:
            return False
        if profile.provider_id == "sandbox":
            return False
        if profile.provider_id not in self.provider_gateway.plugins:
            return False
        if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
            return False
        return True

    def _profile_by_id(self, profile_id: str) -> "ProviderProfile | None":
        reader = getattr(self.provider_gateway, "provider_reader", None)
        if reader is not None:
            profile = reader.get_profile(profile_id)
            if profile is not None:
                return profile
        return self.repository.provider_profiles.get(profile_id)

    def _is_real_lipsync(self, profile) -> bool:
        """A real lipsync path is active only when the profile is enabled, its
        provider plugin is registered, it is NOT the sandbox provider, and its
        secret (if any) is active. Without a secret this returns False, so the
        sandbox pass-through path remains the selected route."""
        if profile is None or profile.capability != "lipsync.video" or not profile.enabled:
            return False
        if profile.provider_id == "sandbox":
            return False
        if profile.provider_id not in self.provider_gateway.plugins:
            return False
        if profile.secret_ref and not self.provider_gateway._secret_is_active(profile.secret_ref):
            return False
        return True

    def resolve_lipsync(self, request: DigitalHumanVideoRequest):
        """Return ``(profile, is_real)`` for the requested lipsync profile.

        ``is_real`` is True only when a real enabled profile + active secret
        exist. Otherwise the caller uses the requested profile as-is, and the
        gateway routes the seeded sandbox provider for ``runninghub.heygem.default``."""
        profile = self._profile_by_id(request.lipsync.provider_profile_id)
        return profile, self._is_real_lipsync(profile)

    def select_lipsync_fallback(self, current_profile, error_message: str) -> "ProviderProfile | None":
        """Mirror the origin asymmetry: HeyGem -> VideoReTalk always; VideoReTalk
        -> HeyGem only on a content-policy error. Returns the first registered,
        enabled, secret-active real profile of the fallback provider, or None."""
        if current_profile is None:
            return None
        target_provider = LIPSYNC_FAILOVER_POLICY.target_provider_id(
            current_profile.provider_id,
            error_message,
        )
        if target_provider is None:
            return None
        for profile in self.repository.provider_profiles.values():
            if profile.provider_id != target_provider:
                continue
            if self._is_real_lipsync(profile):
                return profile
        return None
