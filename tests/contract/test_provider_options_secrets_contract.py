import pytest
from pydantic import ValidationError

from packages.core.contracts import (
    BgmOptions,
    BrollOptions,
    CoverOptions,
    DegradationCode,
    DigitalHumanVideoRequest,
    ErrorCode,
    LipSyncOptions,
    OutputOptions,
    ProviderCapability,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    RetryPolicy,
    RotateSecretRequest,
    SecretRecord,
    SecretStatus,
    StrictnessOptions,
    SubtitleOptions,
    VoiceOptions,
    WarningCode,
)


def test_provider_profile_capability_and_policy_contracts_match_spec():
    schema_ref = ProviderOptionsSchemaRef(
        schema_id="provider.tts.options",
        schema_version="v1",
        dialect="pydantic",
        sha256="sha256:abc",
    )
    profile = ProviderProfile(
        id="profile_tts",
        provider_id="sandbox",
        model_id="tts.local",
        capability="tts.speech",
        display_name="Sandbox TTS",
        environment="local",
        concurrency_key="sandbox:tts.speech",
        timeout_sec=30,
        retry_policy=RetryPolicy(retryable_error_codes=[ErrorCode.provider_timeout]),
        cost_policy_id=None,
        options_schema_ref=schema_ref,
        version="v1",
    )
    capability = ProviderCapability(
        id="cap_tts",
        capability="tts.speech",
        provider_id="sandbox",
        model_id="tts.local",
        display_name="Sandbox TTS",
        input_schema_id="tts.input",
        output_schema_id="tts.output",
        options_schema_id="provider.tts.options",
        supports_async_job=False,
        supports_cancel=False,
        default_timeout_sec=30,
    )

    assert profile.capability == "tts.speech"
    assert profile.retry_policy.retryable_error_codes == [ErrorCode.provider_timeout]
    assert capability.capability == "tts.speech"
    with pytest.raises(ValidationError):
        RetryPolicy(max_attempts=0)


def test_secret_record_rotation_contract_links_old_and_new_records():
    old_secret = SecretRecord(
        id="sec_old",
        provider_id="sandbox",
        environment="local",
        name="API key",
        secret_ref="dev://sec_old",
        status=SecretStatus.rotated,
    )
    new_secret = SecretRecord(
        id="sec_new",
        provider_id="sandbox",
        environment="local",
        name="API key",
        secret_ref="dev://sec_new",
        status=SecretStatus.active,
        rotated_from_secret_id=old_secret.id,
    )

    assert new_secret.rotated_from_secret_id == "sec_old"
    assert RotateSecretRequest(plaintext_secret="next", reason="scheduled").reason == "scheduled"


def test_warning_code_is_single_spec_enum_and_degradation_notice_shape():
    assert {item.value for item in WarningCode} == {
        "broll.skipped_no_material",
        "bgm.skipped_library_unannotated",
        "font.default_used",
        "cover.frame_fallback",
        "timestamp.estimated",
        "cost.unpriced",
        "budget.exceeded",
        # No-silent-fallback surfacing: lipsync provider fallback, BGM loudness
        # probe failure, and selected-font resolution failure now degrade visibly.
        "lipsync.fallback_used",
        "bgm.loudness_probe_failed",
        "font.resolution_failed",
        "subtitle.burn_skipped",
        # Editing-agent (issue #136) falls back to a deterministic selection when no
        # real llm.chat provider is armed; surfaced visibly, never a silent downgrade.
        "editing_agent.deterministic_fallback",
    }
    assert DegradationCode.font_default_used.value == "font.default_used"


def test_request_options_use_spec_field_names_without_escape_hatches():
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="hello",
        voice=VoiceOptions(voice_id="voice_sandbox", emotion="neutral", volume=1.0),
        broll=BrollOptions(enabled=True, max_inserts=2, min_segment_duration=3.0),
        lipsync=LipSyncOptions(provider_profile_id="runninghub.heygem.default"),
        subtitle=SubtitleOptions(enabled=True, style_preset="douyin"),
        bgm=BgmOptions(enabled=False, bgm_id=None),
        cover=CoverOptions(mode="frame"),
        output=OutputOptions(width=1080, height=1920, fps=30),
        strictness=StrictnessOptions(strict_timestamps=True),
    )

    assert request.subtitle.style_preset == "douyin"
    assert request.bgm.bgm_id is None
