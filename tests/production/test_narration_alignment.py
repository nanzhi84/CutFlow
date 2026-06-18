from __future__ import annotations

from datetime import timedelta

import pytest

from packages.ai.gateway import ProviderResult
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    DigitalHumanVideoRequest,
    ErrorCode,
    MediaInfo,
    NodeRun,
    NodeStatus,
    ProviderError,
    ProviderInvocation,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    ProviderStatus,
    RunStatus,
    SignedUrlResponse,
    WarningCode,
    WorkflowRun,
    utcnow,
)
from packages.core.storage.repository import Repository
from packages.core.workflow import NodeExecutionError
from packages.production.pipeline.degradation_policies import ASR_ESTIMATED_FALLBACK_POLICY
from packages.production.pipeline.digital_human import LocalRuntimeAdapter, RunState


class FailingAsrGateway:
    plugins = {"fake.asr": object()}

    def __init__(self) -> None:
        self.invocation_id = "pinv_failed_asr"

    def invoke(self, call):
        invocation = ProviderInvocation(
            id=self.invocation_id,
            case_id=call.case_id,
            run_id=call.run_id,
            node_run_id=call.node_run_id,
            provider_id="fake.asr",
            model_id="fake-asr",
            provider_profile_id=call.provider_profile_id,
            capability_id=call.capability_id,
            status=ProviderStatus.failed,
            error=ProviderError(
                code=ErrorCode.provider_remote_failed,
                message="ASR provider failed.",
                retryable=True,
            ),
        )
        return invocation, None


class SuccessfulAsrGateway:
    plugins = {"fake.asr": object()}

    def __init__(self) -> None:
        self.invocation_id = "pinv_success_asr"
        self.calls = []

    def invoke(self, call):
        self.calls.append(call)
        invocation = ProviderInvocation(
            id=self.invocation_id,
            case_id=call.case_id,
            run_id=call.run_id,
            node_run_id=call.node_run_id,
            provider_id="fake.asr",
            model_id="fake-asr",
            provider_profile_id=call.provider_profile_id,
            capability_id=call.capability_id,
            status=ProviderStatus.succeeded,
        )
        result = ProviderResult(
            output={
                "text": "第一句介绍痛点。第二句说明方案。第三句引导行动。",
                "segments": [
                    {"start": 0.0, "end": 2.0, "text": "第一句介绍痛点。"},
                    {"start": 2.25, "end": 4.0, "text": "第二句说明方案。"},
                    {"start": 4.25, "end": 6.0, "text": "第三句引导行动。"},
                ],
                "source": "asr",
            },
            audio_seconds=2.0,
        )
        return invocation, result


def _workflow_with_failing_asr() -> LocalRuntimeAdapter:
    repository = Repository()
    repository.provider_profiles["fake.asr.profile"] = ProviderProfile(
        id="fake.asr.profile",
        provider_id="fake.asr",
        model_id="fake-asr",
        capability="asr.transcribe",
        display_name="Fake ASR",
        environment="local",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.asr.options"),
    )
    workflow = object.__new__(LocalRuntimeAdapter)
    workflow.repository = repository
    workflow.provider_gateway = FailingAsrGateway()
    return workflow


def _workflow_with_successful_asr() -> tuple[LocalRuntimeAdapter, SuccessfulAsrGateway]:
    repository = Repository()
    repository.provider_profiles["fake.asr.profile"] = ProviderProfile(
        id="fake.asr.profile",
        provider_id="fake.asr",
        model_id="fake-asr",
        capability="asr.transcribe",
        display_name="Fake ASR",
        environment="local",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id="provider.asr.options"),
    )
    gateway = SuccessfulAsrGateway()
    workflow = object.__new__(LocalRuntimeAdapter)
    workflow.repository = repository
    workflow.provider_gateway = gateway
    return workflow, gateway


def _run_state(*, strict_timestamps: bool, tts_uri: str = "https://media.example/tts.mp3") -> RunState:
    request = DigitalHumanVideoRequest(
        case_id="case_demo",
        script="第一句介绍痛点。第二句说明方案。第三句引导行动。",
        voice={"voice_id": "voice_sandbox"},
        strictness={"strict_timestamps": strict_timestamps},
    )
    tts = Artifact(
        id="art_tts",
        case_id="case_demo",
        run_id="run_1",
        node_run_id="nr_tts",
        kind=ArtifactKind.audio_tts,
        uri=tts_uri,
        media_info=MediaInfo(
            media_type="audio",
            codec="mp3",
            format="mp3",
            duration_sec=6.0,
        ),
        payload_schema="uri-only",
    )
    return RunState(request=request, artifacts={ArtifactKind.audio_tts: tts})


def _run() -> WorkflowRun:
    return WorkflowRun(
        id="run_1",
        job_id="job_1",
        case_id="case_demo",
        workflow_template_id="digital_human_v2",
        workflow_version="v1",
        status=RunStatus.running,
    )


def _node_run() -> NodeRun:
    return NodeRun(
        id="nr_alignment",
        run_id="run_1",
        node_id="NarrationAlignment",
        node_version="v1",
        status=NodeStatus.running,
        input_manifest_hash="sha256:test",
    )


def test_narration_alignment_sends_signed_https_url_to_asr(monkeypatch: pytest.MonkeyPatch):
    class FakeObjectStore:
        def signed_url(self, uri):
            assert uri == "s3://cutagent-demo/generated-audio/tts.mp3"
            return SignedUrlResponse(
                url="https://media.example/signed/tts.mp3",
                expires_at=utcnow() + timedelta(minutes=15),
                request_id="req_signed",
            )

    workflow, gateway = _workflow_with_successful_asr()
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store",
        lambda: FakeObjectStore(),
    )

    output = workflow._narration_alignment(
        _run(),
        _node_run(),
        _run_state(
            strict_timestamps=True,
            tts_uri="s3://cutagent-demo/generated-audio/tts.mp3",
        ),
    )

    artifacts_by_kind = {artifact.kind: artifact for artifact in output.artifacts}
    narration = artifacts_by_kind[ArtifactKind.narration_units].payload
    assert gateway.calls[0].input["audio_uri"] == "https://media.example/signed/tts.mp3"
    assert output.provider_invocation_ids == ["pinv_success_asr"]
    assert narration["source"] == "asr"
    assert narration["units"][0]["pause_after_ms"] == 250
    assert narration["units"][0]["portrait_cut_allowed"] is True
    assert narration["units"][0]["boundary_score"] > 0


def test_narration_alignment_uses_script_text_not_asr_typos(monkeypatch: pytest.MonkeyPatch):
    class TypoAsrGateway(SuccessfulAsrGateway):
        def invoke(self, call):
            invocation, result = super().invoke(call)
            result.output["segments"] = [
                {"start": 0.0, "end": 1.3, "text": "第一句介绍统点，"},
                {"start": 1.45, "end": 2.7, "text": "马上就懂"},
                {"start": 2.85, "end": 4.2, "text": "第二句说明方按，"},
                {"start": 4.35, "end": 6.0, "text": "第三句引到行动"},
            ]
            return invocation, result

    class FakeObjectStore:
        def signed_url(self, uri):
            return SignedUrlResponse(
                url="https://media.example/signed/tts.mp3",
                expires_at=utcnow() + timedelta(minutes=15),
                request_id="req_signed",
            )

    workflow, _gateway = _workflow_with_successful_asr()
    workflow.provider_gateway = TypoAsrGateway()
    monkeypatch.setattr(
        "packages.production.pipeline.digital_human.get_object_store",
        lambda: FakeObjectStore(),
    )

    output = workflow._narration_alignment(
        _run(),
        _node_run(),
        _run_state(strict_timestamps=True, tts_uri="s3://cutagent-demo/generated-audio/tts.mp3"),
    )

    narration = {artifact.kind: artifact for artifact in output.artifacts}[ArtifactKind.narration_units].payload
    assert [unit["text"] for unit in narration["units"]] == [
        "第一句介绍痛点。",
        "第二句说明方案。",
        "第三句引导行动。",
    ]
    assert all(
        "统点" not in unit["text"]
        and "方按" not in unit["text"]
        and "引到" not in unit["text"]
        for unit in narration["units"]
    )


def test_narration_alignment_splits_glued_tts_subtitle_by_script_punctuation():
    workflow, _gateway = _workflow_with_successful_asr()
    state = _run_state(strict_timestamps=True)
    state.scratch["tts_subtitle_segments"] = [
        {
            "start": 0.0,
            "end": 6.0,
            "text": "第一句介绍痛点第二句说明方案第三句引导行动",
        }
    ]
    state.scratch["tts_subtitle_invocation_id"] = "pinv_tts"

    output = workflow._narration_alignment(_run(), _node_run(), state)

    narration = {artifact.kind: artifact for artifact in output.artifacts}[ArtifactKind.narration_units].payload
    assert narration["source"] == "tts_subtitle"
    assert output.provider_invocation_ids == ["pinv_tts"]
    assert [unit["text"] for unit in narration["units"]] == [
        "第一句介绍痛点。",
        "第二句说明方案。",
        "第三句引导行动。",
    ]
    assert [unit["start"] for unit in narration["units"]] == [0.0, 2.0, 4.0]


def test_narration_alignment_non_strict_estimates_when_asr_fails():
    workflow = _workflow_with_failing_asr()

    output = workflow._narration_alignment(_run(), _node_run(), _run_state(strict_timestamps=False))

    artifacts_by_kind = {artifact.kind: artifact for artifact in output.artifacts}
    narration = artifacts_by_kind[ArtifactKind.narration_units].payload
    alignment = artifacts_by_kind[ArtifactKind.audio_alignment].payload
    assert output.status == NodeStatus.succeeded
    assert output.provider_invocation_ids == ["pinv_failed_asr"]
    assert output.warnings == [WarningCode.timestamp_estimated]
    assert output.degradations
    assert output.degradations[0].policy_id == ASR_ESTIMATED_FALLBACK_POLICY.id
    assert output.degradations[0].details["reason"] == "asr_unavailable_estimated_fallback"
    assert output.degradations[0].details["provider_invocation_id"] == "pinv_failed_asr"
    assert narration["source"] == "estimated"
    assert narration["strict"] is False
    assert narration["warnings"] == [WarningCode.timestamp_estimated.value]
    assert len(narration["units"]) == 3
    assert len(alignment["segments"]) == 3


def test_narration_alignment_strict_raises_when_asr_fails():
    workflow = _workflow_with_failing_asr()

    with pytest.raises(NodeExecutionError) as exc:
        workflow._narration_alignment(_run(), _node_run(), _run_state(strict_timestamps=True))

    assert exc.value.error.code == ErrorCode.provider_remote_failed
    assert exc.value.error.retryable is True
