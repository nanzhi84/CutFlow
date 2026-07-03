from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.ai.gateway.provider_gateway import ProviderCall, ProviderResult
from packages.core.contracts import (
    Artifact,
    ArtifactKind,
    ArtifactRef,
    JobStatus,
    JobType,
    ProviderOptionsSchemaRef,
    ProviderProfile,
    RunStatus,
    UploadKind,
    UploadSession,
    UploadStatus,
    VoicePreviewRequest,
    VoicePreviewResponse,
    VoiceProfile,
    utcnow,
)
from packages.core.storage.database import (
    ArtifactRow,
    CaseRow,
    JobRow,
    MediaAssetRow,
    NodeRunRow,
    SelectionLedgerRow,
    VoiceProfileRow,
    WorkflowRunRow,
)
from packages.core.storage.repository import Repository
from packages.production import SqlAlchemyProductionRepository


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _profile(provider_id: str, capability: str, model_id: str) -> ProviderProfile:
    return ProviderProfile(
        id=f"{provider_id}.default",
        provider_id=provider_id,
        model_id=model_id,
        capability=capability,
        display_name=f"{provider_id} default",
        environment="local",
        options_schema_ref=ProviderOptionsSchemaRef(schema_id=f"provider.{capability}.options"),
    )


class RecordingSqlAlchemyVoiceRepository:
    def __init__(self) -> None:
        self.voices = {
            "voice_db": VoiceProfile(
                id="voice_db",
                display_name="DB Voice",
                source="builtin",
                provider_profile_id="fake.tts.default",
            )
        }
        self.uploads = {
            "upl_voice_db": UploadSession(
                id="upl_voice_db",
                kind=UploadKind.voice_reference,
                filename="voice.wav",
                content_type="audio/wav",
                size_bytes=123,
                status=UploadStatus.completed,
                object_uri="local://cutagent-local/uploads/voice.wav",
            )
        }
        self.preview_called = False
        self.clone_called = False
        self.persisted_voices: list[str] = []
        self.preview_artifacts: list[str] = []
        self.upserts: list[tuple[str, str]] = []

    def get_voice(self, voice_id: str) -> VoiceProfile | None:
        return self.voices.get(voice_id)

    def persist_provider_voice(self, voice: VoiceProfile) -> VoiceProfile:
        self.persisted_voices.append(voice.id)
        self.voices[voice.id] = voice
        return voice

    def upsert_voice(
        self,
        *,
        voice_id: str,
        display_name: str,
        source: str,
        provider_profile_id: str,
        vendor: str,
        status: str = "ready",
    ) -> tuple[VoiceProfile, bool]:
        created = voice_id not in self.voices
        voice = VoiceProfile(
            id=voice_id,
            display_name=display_name,
            source=source,
            provider_profile_id=provider_profile_id,
            vendor=vendor,
            status=status,
        )
        self.voices[voice_id] = voice
        self.upserts.append((voice_id, status))
        return voice, created

    def update_provider_preview(self, voice_id: str, artifact: Artifact) -> ArtifactRef:
        self.preview_artifacts.append(artifact.id)
        voice = self.voices[voice_id]
        self.voices[voice_id] = voice.model_copy(
            update={"preview_artifact_id": artifact.id, "updated_at": utcnow()}
        )
        return ArtifactRef(
            artifact_id=artifact.id,
            kind=artifact.kind,
            uri=artifact.uri or f"artifact://{artifact.id}",
            schema_version=artifact.schema_version,
            sha256=artifact.sha256,
        )

    def hydrate_voice_reference_upload(self, repository: Repository, upload_id: str) -> None:
        repository.uploads[upload_id] = self.uploads[upload_id]

    def preview_voice(self, voice_id: str, payload: VoicePreviewRequest) -> VoicePreviewResponse | None:
        self.preview_called = True
        raise AssertionError("SQLAlchemy provider preview must not use sandbox media repository")

    def clone_voice(self, payload):
        self.clone_called = True
        raise AssertionError("SQLAlchemy provider clone must not use sandbox media repository")


class RecordingTTSProvider:
    provider_id = "fake.tts"

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.calls: list[ProviderCall] = []
        self.artifact_id: str | None = None

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        artifact = self.repository.create_artifact(
            kind=ArtifactKind.audio_tts,
            payload_schema="VoicePreviewArtifact.v1",
            payload={"text": call.input["text"], "voice_id": call.input["voice_id"]},
            uri="local://cutagent-local/provider/voice-preview.wav",
        )
        self.artifact_id = artifact.id
        return ProviderResult(
            output={"audio_artifact_id": artifact.id, "audio_uri": artifact.uri, "duration_sec": 1.25},
            audio_seconds=1.25,
        )


class RecordingVoiceBuildProvider:
    provider_id = "fake.voice"

    def __init__(self, repository: Repository) -> None:
        self.repository = repository
        self.calls: list[ProviderCall] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        operation = str(call.input["operation"])
        artifact = self.repository.create_artifact(
            kind=ArtifactKind.audio_tts,
            payload_schema="VoicePreviewArtifact.v1",
            payload={"operation": operation},
            uri=f"local://cutagent-local/provider/{operation}-preview.wav",
        )
        return ProviderResult(
            output={
                "voice_id": f"voice_provider_{operation}",
                "preview_audio_artifact_id": artifact.id,
            }
        )


class VoiceOperationsProvider:
    provider_id = "fake.voiceops"

    def __init__(self) -> None:
        self.calls: list[ProviderCall] = []

    def invoke(self, call: ProviderCall) -> ProviderResult:
        self.calls.append(call)
        operation = str(call.input.get("operation") or "")
        if operation == "voice_list":
            return ProviderResult(
                output={
                    "voices": [
                        {
                            "voice_id": "voice_remote_a",
                            "display_name": "Remote A",
                            "source": "designed",
                            "status": "training",
                        },
                        {
                            "voice_id": "voice_remote_b",
                            "display_name": "",
                            "source": "legacy-source",
                            "status": "mystery",
                        },
                        {"voice_id": ""},
                        "not a dict",
                    ]
                }
            )
        if operation == "train_status":
            return ProviderResult(output={"status": "ready"})
        raise AssertionError(f"unexpected voice operation: {operation}")


def test_sqlalchemy_voice_preview_uses_gateway_and_persists_provider_artifact():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        media_repository = RecordingSqlAlchemyVoiceRepository()
        provider = RecordingTTSProvider(repository)
        client.app.state.sqlalchemy_media_repository = media_repository
        client.app.state.provider_gateway.register(provider)
        repository.provider_profiles["fake.tts.default"] = _profile("fake.tts", "tts.speech", "fake-tts")

        response = client.post("/api/voices/voice_db/preview", json={"text": "DB provider preview"})

        assert response.status_code == 200, response.text
        assert response.json()["audio_artifact"]["artifact_id"] == provider.artifact_id
        assert response.json()["audio_artifact"]["uri"].startswith("local://")
        assert provider.calls[0].provider_profile_id == "fake.tts.default"
        assert provider.calls[0].input["voice_id"] == "voice_db"
        assert media_repository.preview_called is False
        assert media_repository.preview_artifacts == [provider.artifact_id]
        assert media_repository.voices["voice_db"].preview_artifact_id == provider.artifact_id


def test_sqlalchemy_voice_clone_uses_gateway_and_persist_provider_voice():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        media_repository = RecordingSqlAlchemyVoiceRepository()
        provider = RecordingVoiceBuildProvider(repository)
        client.app.state.sqlalchemy_media_repository = media_repository
        client.app.state.provider_gateway.register(provider)
        repository.provider_profiles["fake.voice.default"] = _profile("fake.voice", "tts.speech", "fake-voice")

        clone = client.post(
            "/api/voices/clone",
            json={
                "display_name": "Provider Clone",
                "reference_upload_session_id": "upl_voice_db",
                "provider_profile_id": "fake.voice.default",
            },
        )

        assert clone.status_code == 202, clone.text
        assert clone.json()["id"] == "voice_provider_clone"
        assert media_repository.clone_called is False
        assert media_repository.persisted_voices == ["voice_provider_clone"]
        assert "upl_voice_db" in repository.uploads
        assert [call.input["operation"] for call in provider.calls] == ["clone"]


def test_sync_voices_normalizes_provider_voice_list():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        media_repository = RecordingSqlAlchemyVoiceRepository()
        provider = VoiceOperationsProvider()
        client.app.state.sqlalchemy_media_repository = media_repository
        client.app.state.provider_gateway.register(provider)
        repository.provider_profiles["fake.voiceops.default"] = _profile(
            "fake.voiceops", "tts.speech", "fake-voiceops"
        )

        response = client.post(
            "/api/voices/sync",
            json={"provider_profile_id": "fake.voiceops.default"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["imported"] == 2
        assert body["updated"] == 0
        assert body["total"] == 2
        assert [call.input["operation"] for call in provider.calls] == ["voice_list"]
        assert media_repository.voices["voice_remote_a"].source == "designed"
        assert media_repository.voices["voice_remote_a"].status == "training"
        assert media_repository.voices["voice_remote_b"].display_name == "voice_remote_b"
        assert media_repository.voices["voice_remote_b"].source == "cloned"
        assert media_repository.voices["voice_remote_b"].status == "ready"
        assert media_repository.voices["voice_remote_b"].vendor == "fake"


def test_refresh_training_voice_status_uses_provider_train_status():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        media_repository = RecordingSqlAlchemyVoiceRepository()
        media_repository.voices["voice_training"] = VoiceProfile(
            id="voice_training",
            display_name="Training voice",
            source="cloned",
            provider_profile_id="fake.voiceops.default",
            vendor="fake",
            status="training",
        )
        provider = VoiceOperationsProvider()
        client.app.state.sqlalchemy_media_repository = media_repository
        client.app.state.provider_gateway.register(provider)
        repository.provider_profiles["fake.voiceops.default"] = _profile(
            "fake.voiceops", "tts.speech", "fake-voiceops"
        )

        response = client.post("/api/voices/voice_training/refresh-status")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready"
        assert [call.input["operation"] for call in provider.calls] == ["train_status"]
        assert media_repository.upserts == [("voice_training", "ready")]


def test_voice_preview_fails_fast_when_sandbox_fallback_is_disabled(monkeypatch):
    monkeypatch.setenv("CUTAGENT_ALLOW_SANDBOX_FALLBACK", "0")
    with TestClient(create_app()) as client:
        _login_admin(client)
        media_repository = RecordingSqlAlchemyVoiceRepository()
        media_repository.voices["voice_no_provider"] = VoiceProfile(
            id="voice_no_provider",
            display_name="No provider",
            source="cloned",
            provider_profile_id="missing.tts.default",
        )
        client.app.state.sqlalchemy_media_repository = media_repository

        response = client.post("/api/voices/voice_no_provider/preview", json={"text": "试听"})

        assert response.status_code == 400
        assert response.json()["error"]["code"] == "provider.unsupported_option"
        assert media_repository.preview_called is False


class StaticHydrateSession:
    def __init__(self, rows_by_model: dict[type, list[object]], rows_by_key: dict[tuple[type, str], object]) -> None:
        self.rows_by_model = rows_by_model
        self.rows_by_key = rows_by_key
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        return None

    def get(self, model, key):
        return self.rows_by_key.get((model, key))

    def scalars(self, statement):
        self.statements.append(statement)
        entity = statement.column_descriptions[0]["entity"]
        return self.rows_by_model.get(entity, [])


def _timestamped(row):
    now = utcnow()
    row.schema_version = "v1"
    row.created_at = now
    row.updated_at = now
    return row


def test_hydrate_workflow_runtime_snapshot_loads_db_voice_profiles():
    job = _timestamped(
        JobRow(
            id="job_db_voice",
            type=JobType.digital_human_video.value,
            status=JobStatus.queued.value,
            case_id="case_demo",
            created_by="usr_admin",
            request_schema="DigitalHumanVideoRequest.v1",
            request={
                "case_id": "case_demo",
                "title": "DB voice run",
                "script": "数据库音色需要进入 worker。",
                "voice": {"voice_id": "voice_db"},
                "strictness": {"strict_timestamps": False},
            },
        )
    )
    run = _timestamped(
        WorkflowRunRow(
            id="run_db_voice",
            job_id=job.id,
            case_id="case_demo",
            workflow_template_id="digital_human_video",
            workflow_version="v1",
            status=RunStatus.admitted.value,
            requested_by="usr_admin",
            run_attempt=1,
        )
    )
    case = _timestamped(
        CaseRow(
            id="case_demo",
            name="Demo Case",
            owner_user_id="usr_admin",
            status="active",
            description=None,
            industry=None,
            product=None,
            target_audience=None,
        )
    )
    voice = _timestamped(
        VoiceProfileRow(
            id="voice_db",
            display_name="DB Voice",
            source="builtin",
            provider_profile_id="fake.tts.default",
            enabled=True,
        )
    )
    rows_by_model = {NodeRunRow: [], VoiceProfileRow: [voice]}
    rows_by_key = {
        (JobRow, job.id): job,
        (WorkflowRunRow, run.id): run,
        (CaseRow, case.id): case,
    }
    production_repository = SqlAlchemyProductionRepository(lambda: StaticHydrateSession(rows_by_model, rows_by_key))
    runtime_repository = Repository()

    production_repository.hydrate_workflow_runtime_snapshot(runtime_repository, run.id)

    assert runtime_repository.voices["voice_db"].provider_profile_id == "fake.tts.default"


def test_hydrate_workflow_runtime_snapshot_loads_case_selection_ledger():
    job = _timestamped(
        JobRow(
            id="job_bgm_ledger",
            type=JobType.digital_human_video.value,
            status=JobStatus.queued.value,
            case_id="case_demo",
            created_by="usr_admin",
            request_schema="DigitalHumanVideoRequest.v1",
            request={
                "case_id": "case_demo",
                "script": "数据库 ledger 需要进入 worker。",
                "voice": {"voice_id": "voice_demo_cn"},
                "strictness": {"strict_timestamps": False},
            },
        )
    )
    run = _timestamped(
        WorkflowRunRow(
            id="run_bgm_ledger",
            job_id=job.id,
            case_id="case_demo",
            workflow_template_id="digital_human_video",
            workflow_version="v1",
            status=RunStatus.admitted.value,
            requested_by="usr_admin",
            run_attempt=1,
        )
    )
    case = _timestamped(
        CaseRow(
            id="case_demo",
            name="Demo Case",
            owner_user_id="usr_admin",
            status="active",
            description=None,
            industry=None,
            product=None,
            target_audience=None,
        )
    )
    ledger = SelectionLedgerRow(
        id="sel_bgm_segment_1",
        case_id="case_demo",
        run_id="run_previous",
        medium="bgm",
        asset_id="asset_bgm_song",
        clip_id="bgm_segment_1",
        slot_phase="bgm",
        diversity_key=None,
        created_at=utcnow(),
    )
    rows_by_model = {NodeRunRow: [], VoiceProfileRow: [], SelectionLedgerRow: [ledger]}
    rows_by_key = {
        (JobRow, job.id): job,
        (WorkflowRunRow, run.id): run,
        (CaseRow, case.id): case,
    }
    production_repository = SqlAlchemyProductionRepository(lambda: StaticHydrateSession(rows_by_model, rows_by_key))
    runtime_repository = Repository()

    production_repository.hydrate_workflow_runtime_snapshot(runtime_repository, run.id)

    recent = runtime_repository.recent_selections(case_id="case_demo", medium="bgm")
    assert [(entry.asset_id, entry.clip_id) for entry in recent] == [
        ("asset_bgm_song", "bgm_segment_1")
    ]


def test_hydrate_workflow_runtime_snapshot_loads_case_media_asset_source_artifact():
    job = _timestamped(
        JobRow(
            id="job_db_portrait",
            type=JobType.digital_human_video.value,
            status=JobStatus.queued.value,
            case_id="case_demo",
            created_by="usr_admin",
            request_schema="DigitalHumanVideoRequest.v1",
            request={
                "case_id": "case_demo",
                "title": "DB portrait run",
                "script": "真人素材需要进入 worker。",
                "voice": {"voice_id": "voice_demo_cn"},
                "strictness": {"strict_timestamps": False},
            },
        )
    )
    run = _timestamped(
        WorkflowRunRow(
            id="run_db_portrait",
            job_id=job.id,
            case_id="case_demo",
            workflow_template_id="digital_human_video",
            workflow_version="v1",
            status=RunStatus.admitted.value,
            requested_by="usr_admin",
            run_attempt=1,
        )
    )
    case = _timestamped(
        CaseRow(
            id="case_demo",
            name="Demo Case",
            owner_user_id="usr_admin",
            status="active",
            description=None,
            industry=None,
            product=None,
            target_audience=None,
        )
    )
    source_artifact = _timestamped(
        ArtifactRow(
            id="art_realportrait001",
            case_id="case_demo",
            run_id=None,
            node_run_id=None,
            kind=ArtifactKind.uploaded_file.value,
            uri="local://cutagent-local/uploads/real-portrait.mp4",
            local_path=None,
            oss_uri=None,
            size_bytes=1024,
            immutable=True,
            retention_policy="default",
            sha256="sha256-real-portrait",
            media_info={
                "media_type": "video",
                "codec": "h264",
                "format": "mp4",
                "duration_sec": 3.0,
            },
            payload_schema="UploadedFileArtifact.v1",
            payload={"filename": "real-portrait.mp4"},
            created_by_node_run_id=None,
        )
    )
    media_asset = _timestamped(
        MediaAssetRow(
            id="asset_portrait_demo",
            case_id="case_demo",
            title="Real portrait",
            kind="video",
            source_artifact_id=source_artifact.id,
            tags=["portrait"],
            annotation_status="annotated",
            usable=True,
        )
    )
    rows_by_model = {
        NodeRunRow: [],
        VoiceProfileRow: [],
        MediaAssetRow: [media_asset],
        # Source artifacts are now batch-loaded via a single IN query
        # (select(ArtifactRow).where(id.in_(...))) instead of per-asset
        # session.get, so the fake serves them through rows_by_model.
        ArtifactRow: [source_artifact],
    }
    rows_by_key = {
        (JobRow, job.id): job,
        (WorkflowRunRow, run.id): run,
        (CaseRow, case.id): case,
    }
    production_repository = SqlAlchemyProductionRepository(
        lambda: StaticHydrateSession(rows_by_model, rows_by_key)
    )
    runtime_repository = Repository()

    production_repository.hydrate_workflow_runtime_snapshot(runtime_repository, run.id)

    assert runtime_repository.media_assets[media_asset.id].source_artifact_id == source_artifact.id
    assert runtime_repository.artifacts[source_artifact.id].uri == "local://cutagent-local/uploads/real-portrait.mp4"


def test_hydrate_workflow_runtime_snapshot_queries_global_media_assets():
    job = _timestamped(
        JobRow(
            id="job_db_global_bgm",
            type=JobType.digital_human_video.value,
            status=JobStatus.queued.value,
            case_id="case_demo",
            created_by="usr_admin",
            request_schema="DigitalHumanVideoRequest.v1",
            request={
                "case_id": "case_demo",
                "title": "DB BGM run",
                "script": "全局 BGM 也需要进入 worker。",
                "voice": {"voice_id": "voice_demo_cn"},
                "strictness": {"strict_timestamps": False},
            },
        )
    )
    run = _timestamped(
        WorkflowRunRow(
            id="run_db_global_bgm",
            job_id=job.id,
            case_id="case_demo",
            workflow_template_id="digital_human_video",
            workflow_version="v1",
            status=RunStatus.admitted.value,
            requested_by="usr_admin",
            run_attempt=1,
        )
    )
    case = _timestamped(
        CaseRow(
            id="case_demo",
            name="Demo Case",
            owner_user_id="usr_admin",
            status="active",
            description=None,
            industry=None,
            product=None,
            target_audience=None,
        )
    )
    global_bgm_artifact = _timestamped(
        ArtifactRow(
            id="art_global_bgm",
            case_id=None,
            run_id=None,
            node_run_id=None,
            kind=ArtifactKind.uploaded_file.value,
            uri="local://cutagent-local/uploads/global-bgm.mp3",
            local_path=None,
            oss_uri=None,
            size_bytes=1024,
            immutable=True,
            retention_policy="default",
            sha256="sha256-global-bgm",
            media_info={"media_type": "audio", "codec": "mp3", "format": "mp3", "duration_sec": 10.0},
            payload_schema="UploadedFileArtifact.v1",
            payload={"filename": "global-bgm.mp3"},
            created_by_node_run_id=None,
        )
    )
    global_bgm = _timestamped(
        MediaAssetRow(
            id="asset_global_bgm",
            case_id=None,
            title="Global BGM",
            kind="bgm",
            source_artifact_id=global_bgm_artifact.id,
            tags=["bgm"],
            annotation_status="annotated",
            usable=True,
        )
    )
    session = StaticHydrateSession(
        rows_by_model={
            NodeRunRow: [],
            VoiceProfileRow: [],
            MediaAssetRow: [global_bgm],
            # Batch-loaded via a single IN query now (see note above), so the
            # fake serves the source artifact through rows_by_model.
            ArtifactRow: [global_bgm_artifact],
        },
        rows_by_key={
            (JobRow, job.id): job,
            (WorkflowRunRow, run.id): run,
            (CaseRow, case.id): case,
        },
    )
    production_repository = SqlAlchemyProductionRepository(lambda: session)
    runtime_repository = Repository()

    production_repository.hydrate_workflow_runtime_snapshot(runtime_repository, run.id)

    media_statements = [
        str(statement)
        for statement in session.statements
        if statement.column_descriptions[0]["entity"] is MediaAssetRow
    ]
    assert any("media_assets.case_id IS NULL" in statement for statement in media_statements)
    assert runtime_repository.media_assets[global_bgm.id].case_id is None
    assert runtime_repository.artifacts[global_bgm_artifact.id].uri == "local://cutagent-local/uploads/global-bgm.mp3"


def test_tts_node_uses_voice_provider_profile_when_request_omits_override():
    with TestClient(create_app()) as client:
        _login_admin(client)
        repository = client.app.state.repository
        provider = RecordingTTSProvider(repository)
        client.app.state.provider_gateway.register(provider)
        repository.provider_profiles["fake.tts.default"] = _profile("fake.tts", "tts.speech", "fake-tts")
        repository.voices["voice_db"] = VoiceProfile(
            id="voice_db",
            display_name="DB Voice",
            source="builtin",
            provider_profile_id="fake.tts.default",
        )

        response = client.post(
            "/api/jobs/digital-human-video",
            json={
                "case_id": "case_demo",
                "title": "Voice profile routing",
                "script": "音色自带 provider 应该驱动 TTS。",
                "voice": {"voice_id": "voice_db"},
                "broll": {"enabled": False},
                "bgm": {"enabled": False},
                "subtitle": {"enabled": True},
                "lipsync": {"enabled": False},
                "strictness": {"strict_timestamps": False},
            },
        )

        assert response.status_code == 201, response.text
        run_id = response.json()["initial_run"]["id"]
        tts_node = next(node for node in repository.node_runs[run_id] if node.node_id == "TTS")
        assert provider.calls[0].provider_profile_id == "fake.tts.default"
        assert provider.calls[0].input["voice_id"] == "voice_db"
        assert tts_node.output_artifact_ids == [provider.artifact_id]
