import json
import zipfile

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.main import app
from apps.api.main import repository
from packages.ai.gateway.provider_gateway import ProviderRuntimeError, SandboxProvider
from packages.core.contracts import (
    AnnotationEditorVm,
    AnnotationMetaV4,
    AnnotationV4,
    ArtifactKind,
    ClipRetrievalV4,
    ClipSemanticsV4,
    ClipUsageV4,
    ClipV4,
    ErrorCode,
    UsageRole,
    UsageWindowV4,
)
from packages.core.storage.object_store import get_object_store, parse_object_uri
from packages.media.assets import local_object_path
from packages.media.video.ffmpeg import probe_media, probe_stream_types, probe_video_frame_count


client = TestClient(app)


def login_admin():
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def login_admin_for(active_client):
    response = active_client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def fresh_client():
    return TestClient(create_app())


def video_payload(**overrides):
    payload = {
        "case_id": "case_demo",
        "title": "Golden success",
        "script": "先指出低效内容生产的痛点。再展示 Case Memory 如何复用历史经验。最后邀请运营查看报告。",
        "voice": {"voice_id": "voice_sandbox"},
        "portrait": {"template_mode": "agent"},
        "broll": {"enabled": False, "max_inserts": 2},
        "bgm": {"enabled": False},
        "subtitle": {"enabled": True},
        "lipsync": {"enabled": True, "provider_profile_id": "runninghub.heygem.default"},
        "strictness": {"strict_timestamps": False},
    }
    payload.update(overrides)
    return payload


def run_report(active_client, run_id):
    response = active_client.get(f"/api/runs/{run_id}/report")
    assert response.status_code == 200, response.text
    return response.json()


def node_errors(active_client, run_id):
    detail = active_client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200, detail.text
    return [node.get("error") for node in detail.json()["node_runs"] if node.get("error")]


class FailingOnceLipSyncSandbox(SandboxProvider):
    def __init__(self, code: ErrorCode) -> None:
        self.code = code
        self.failed_once = False

    def invoke(self, call):
        if call.capability_id == "lipsync.video" and not self.failed_once:
            self.failed_once = True
            raise ProviderRuntimeError(self.code, f"Simulated {self.code.value}")
        return super().invoke(call)


def test_minimal_success_video_creates_finished_video_and_report():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(),
        headers={"Idempotency-Key": "golden-video-success"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    replayed = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(),
        headers={"Idempotency-Key": "golden-video-success"},
    )
    assert replayed.status_code == 200, replayed.text  # spec 32.11: replay -> 200
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert replayed.json()["job"]["id"] == body["job"]["id"]
    assert replayed.json()["initial_run"]["id"] == body["initial_run"]["id"]
    run = body["initial_run"]
    assert run["status"] == "succeeded"
    report = client.get(f"/api/runs/{run['id']}/report").json()
    assert report["public_report"]["status"] == "succeeded"
    videos = client.get("/api/cases/case_demo/finished-videos").json()["items"]
    assert videos


def test_case_run_cards_list_recent_runs_for_case():
    with fresh_client() as active_client:
        login_admin_for(active_client)
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="Run card list"),
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]

        listed = active_client.get("/api/cases/case_demo/runs")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        card = body["items"][0]
        assert card["runId"] == run["id"]
        assert card["jobId"] == run["job_id"]
        assert card["caseId"] == "case_demo"
        # A completed run's card now surfaces the finished video's generated headline
        # (here the deterministic first-sentence derivation, since the golden flow has
        # no real LLM armed) rather than the raw request title.
        finished_videos = active_client.get("/api/cases/case_demo/finished-videos").json()["items"]
        assert finished_videos, "completed run should have produced a finished video"
        assert card["title"] == finished_videos[-1]["title"]
        assert card["title"] == "先指出低效内容生产的痛点"
        assert card["progress"] == 1
        assert card["canPublish"] is True
        assert card["canRetry"] is False
        assert "warnings" in card


def _annotate_broll_demo(repository, *, keywords, scene_type="工具展示", duration=4.0):
    """Attach a real AnnotationV4 (one clip + a usage window) to the seed b-roll.

    The sandbox seed asset is flagged annotated but carries no real V4 payload, so
    b-roll honestly soft-degrades; tests that exercise real matching must inject a
    genuine annotation (clip semantics/keywords + a recommended usage window).
    """
    asset = repository.media_assets["asset_broll_demo"]
    annotation = AnnotationV4(
        meta=AnnotationMetaV4(
            asset_id=asset.id,
            case_id=asset.case_id or "case_demo",
            material_type="broll",
            duration=duration,
        ),
        clips=[
            ClipV4(
                segment_id="clip_demo_1",
                start=0.0,
                end=duration,
                duration=duration,
                semantics=ClipSemanticsV4(scene_type=scene_type, narrative_role="效果展示"),
                usage=ClipUsageV4(role=UsageRole.cover),
                retrieval=ClipRetrievalV4(
                    summary=" ".join(keywords),
                    keywords=list(keywords),
                    retrieval_sentence=" ".join(keywords),
                ),
            )
        ],
        usage_windows=[
            UsageWindowV4(start=0.0, end=duration, role=UsageRole.cover, confidence=0.9)
        ],
        quality_report={"usable_ratio": 0.9},
    )
    repository.annotations[asset.id] = AnnotationEditorVm(
        asset=asset,
        etag="etag-broll-demo",
        canonical=annotation.model_dump(mode="json"),
        projection={"usable": True},
    )


def test_spec_20_2_2_broll_enabled_with_real_annotation_creates_non_empty_plan():
    """Spec 20.2 #2: B-roll enabled success — REAL matching, not a seeded pick.

    With a genuine AnnotationV4 whose clip keywords match the script, b-roll
    planning produces real inserts anchored inside narration windows (not the
    old 0/3/6 grid), with matched keywords populated and a real score.
    """
    with fresh_client() as active_client:
        login_admin_for(active_client)
        repo = active_client.app.state.repository
        _annotate_broll_demo(repo, keywords=["报告", "运营", "经验"])
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="B-roll success", broll={"enabled": True, "max_inserts": 1}),
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "succeeded"
        report = run_report(active_client, run["id"])["public_report"]
        assert "broll.skipped_no_material" not in report["degradations"]
        artifacts = {
            artifact.kind: artifact
            for artifact in active_client.app.state.repository.artifacts.values()
            if artifact.run_id == run["id"]
        }
        broll_plan = artifacts[ArtifactKind.plan_broll].payload
        assert broll_plan["enabled"] is True
        assert broll_plan["segments"]
        segment = broll_plan["segments"][0]
        # Real placement: anchored inside a narration window, not start_sec=0.
        narration = artifacts[ArtifactKind.narration_units].payload["units"]
        assert any(
            unit["start"] <= segment["start_sec"] < unit["end"] for unit in narration
        )
        # Real matching: keyword overlap is surfaced, not a fabricated pick.
        assert segment["matched_keywords"]


def test_spec_20_2_2b_broll_enabled_without_annotation_soft_degrades():
    """Honest soft-degrade: the sandbox seed b-roll has no real annotation, so an
    enabled b-roll request degrades with broll.skipped_no_material rather than a
    fabricated insert."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="B-roll no annotation", broll={"enabled": True, "max_inserts": 1}),
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "succeeded"
        report = run_report(active_client, run["id"])["public_report"]
        assert "broll.skipped_no_material" in report["degradations"]
        artifacts = {
            artifact.kind: artifact
            for artifact in active_client.app.state.repository.artifacts.values()
            if artifact.run_id == run["id"]
        }
        broll_plan = artifacts[ArtifactKind.plan_broll].payload
        assert broll_plan["enabled"] is True
        assert broll_plan["segments"] == []
        assert broll_plan["skipped_reason"] == "broll.skipped_no_material"


def test_broll_missing_is_soft_degrade_and_reported():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(
            title="B-roll degraded",
            broll={"enabled": True, "max_inserts": 2, "case_id": "case_without_broll"},
        ),
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]
    assert run["status"] == "succeeded"
    report = client.get(f"/api/runs/{run['id']}/report").json()["public_report"]
    assert "broll.skipped_no_material" in report["degradations"]


def test_spec_20_2_4_bgm_missing_is_soft_degrade_and_reported_with_warning_code():
    """Spec 20.2 #4 / spec 2.2: BGM unavailable degrades with bgm.skipped_library_unannotated."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        repo = active_client.app.state.repository
        repo.media_assets["asset_bgm_demo"] = repo.media_assets["asset_bgm_demo"].model_copy(
            update={"usable": False}
        )
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="BGM degraded", bgm={"enabled": True}),
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "succeeded"
        report = run_report(active_client, run["id"])["public_report"]
        assert "bgm.skipped_library_unannotated" in report["warnings"]
        assert "bgm.skipped_library_unannotated" in report["degradations"]


def test_bgm_enabled_with_seed_asset_mixes_audio_into_final_video():
    with fresh_client() as active_client:
        login_admin_for(active_client)
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="BGM mixed", bgm={"enabled": True, "volume": 0.2}),
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "succeeded"
        report = run_report(active_client, run["id"])["public_report"]
        assert "bgm.skipped_library_unannotated" not in report["degradations"]
        artifacts = {
            artifact.kind: artifact
            for artifact in active_client.app.state.repository.artifacts.values()
            if artifact.run_id == run["id"]
        }
        final = artifacts[ArtifactKind.video_final]
        assert final.uri
        assert "audio" in probe_stream_types(local_object_path(get_object_store(), final.uri))


def test_portrait_missing_is_hard_fail():
    login_admin()
    case = client.post("/api/cases", json={"name": "No portrait case"}).json()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(case_id=case["id"], title="Hard fail"),
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]
    assert run["status"] == "failed"
    detail = client.get(f"/api/runs/{run['id']}").json()
    errors = [node.get("error") for node in detail["node_runs"] if node.get("error")]
    assert errors[-1]["code"] == "material.insufficient.portrait"
    report = client.get(f"/api/runs/{run['id']}/report").json()
    assert report["debug_report"]["node_errors"][-1]["code"] == "material.insufficient.portrait"


def test_spec_20_2_6_lipsync_timeout_can_resume_reusing_valid_prefix():
    """Spec 20.2 #6 / spec 2.3: provider.timeout fails first run, then resume reuses valid prefix."""
    with fresh_client() as active_client:
        active_client.app.state.provider_gateway.plugins["sandbox"] = FailingOnceLipSyncSandbox(
            ErrorCode.provider_timeout
        )
        login_admin_for(active_client)
        failed = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="LipSync timeout resume"),
        )
        assert failed.status_code == 201, failed.text
        failed_run = failed.json()["initial_run"]
        assert failed_run["status"] == "failed"
        assert node_errors(active_client, failed_run["id"])[-1]["code"] == "provider.timeout"
        report = run_report(active_client, failed_run["id"])
        assert report["debug_report"]["node_errors"][-1]["code"] == "provider.timeout"
        assert report["debug_report"]["node_errors"][-1]["retryable"] is True

        resumed = active_client.post(
            f"/api/runs/{failed_run['id']}/resume",
            json={"reason": "resume after sandbox timeout", "reuse_valid_artifacts": True},
        )
        assert resumed.status_code == 201, resumed.text
        resumed_run = resumed.json()["run"]
        assert resumed_run["status"] == "succeeded"
        detail = active_client.get(f"/api/runs/{resumed_run['id']}").json()
        skipped = [node["node_id"] for node in detail["node_runs"] if node["status"] == "skipped"]
        assert {"ValidateRequest", "LoadCaseContext", "ResolveCreativeIntent", "TTS"} <= set(skipped)


def test_spec_20_2_7_provider_quota_exceeded_is_retryable_hard_fail():
    """Spec 20.2 #7 / spec 2.3: provider quota exhaustion reports provider.quota_exceeded."""
    with fresh_client() as active_client:
        active_client.app.state.provider_gateway.plugins["sandbox"] = FailingOnceLipSyncSandbox(
            ErrorCode.provider_quota_exceeded
        )
        login_admin_for(active_client)
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="Quota exceeded"),
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "failed"
        error = node_errors(active_client, run["id"])[-1]
        assert error["code"] == "provider.quota_exceeded"
        assert error["retryable"] is True
        report = run_report(active_client, run["id"])
        assert report["debug_report"]["node_errors"][-1]["code"] == "provider.quota_exceeded"


def test_spec_20_2_8_broll_inserts_stay_in_bounds_on_short_timeline():
    """Spec 20.2 #8 (honest): a real b-roll insert is clamped to the narration
    window, so even a short script never yields an out-of-bounds timeline.

    Previously a fabricated start_sec=index*3 placement pushed the insert past a
    short timeline and tripped render.invalid_timeline. With real planning the
    insert is anchored inside a real narration window and trimmed to fit, so the
    run succeeds and the b-roll segment stays in bounds — the out-of-bounds path
    can no longer be reached via a fake placement.
    """
    with fresh_client() as active_client:
        login_admin_for(active_client)
        repo = active_client.app.state.repository
        _annotate_broll_demo(repo, keywords=["效果", "展示"], duration=4.0)
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(
                title="Short timeline broll",
                script="展示效果。",
                broll={"enabled": True, "max_inserts": 1},
            ),
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "succeeded"
        artifacts = {
            artifact.kind: artifact
            for artifact in active_client.app.state.repository.artifacts.values()
            if artifact.run_id == run["id"]
        }
        timeline = artifacts[ArtifactKind.plan_timeline].payload
        assert timeline["validation"]["checks"]["out_of_bounds"] is True
        total_frames = timeline["total_frames"]
        broll_tracks = [t for t in timeline["tracks"] if t["track_id"] == "broll"]
        for track in broll_tracks:
            assert track["timeline_end_frame"] <= total_frames


def test_spec_20_2_9_subtitle_enabled_creates_artifact_and_disabled_omits_it():
    """Spec 20.2 #9: subtitle.ass exists only when subtitles are enabled."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        enabled = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="Subtitle enabled", subtitle={"enabled": True}),
        )
        assert enabled.status_code == 201, enabled.text
        enabled_run = enabled.json()["initial_run"]
        assert enabled_run["status"] == "succeeded"
        enabled_kinds = {
            ref["kind"] for ref in active_client.get(f"/api/runs/{enabled_run['id']}/artifacts").json()["artifacts"]
        }
        assert "subtitle.ass" in enabled_kinds

        disabled = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="Subtitle disabled", subtitle={"enabled": False}),
        )
        assert disabled.status_code == 201, disabled.text
        disabled_run = disabled.json()["initial_run"]
        assert disabled_run["status"] == "succeeded"
        disabled_kinds = {
            ref["kind"] for ref in active_client.get(f"/api/runs/{disabled_run['id']}/artifacts").json()["artifacts"]
        }
        assert "subtitle.ass" not in disabled_kinds


def test_spec_20_2_10_editor_handoff_and_jianying_draft_exports_have_package_artifacts():
    """Spec 20.2 #10: editor handoff and Jianying draft export package artifacts."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(title="Editor package exports"),
        )
        assert response.status_code == 201, response.text
        videos = active_client.get("/api/cases/case_demo/finished-videos").json()["items"]
        finished_video_id = videos[-1]["id"]

        handoff = active_client.post(
            f"/api/finished-videos/{finished_video_id}/editor-handoff",
            json={"format": "zip"},
        )
        assert handoff.status_code == 201, handoff.text
        handoff_body = handoff.json()
        assert handoff_body["package_artifact"]["kind"] == "editor.handoff_package"
        assert handoff_body["package_artifact"]["uri"].startswith("local://")
        assert handoff_body["manifest"]["finished_video_id"] == finished_video_id
        assert handoff_body["manifest"]["package_uri"] == handoff_body["package_artifact"]["uri"]
        assert handoff_body["manifest"]["assets"]["video"]

        jianying = active_client.post(
            f"/api/finished-videos/{finished_video_id}/jianying-draft",
            json={"template_id": "clean-template"},
        )
        assert jianying.status_code == 201, jianying.text
        jianying_body = jianying.json()
        assert jianying_body["package_artifact"]["kind"] == "editor.jianying_draft_package"
        assert jianying_body["package_artifact"]["uri"].startswith("local://")
        assert jianying_body["draft_manifest"]["template_id"] == "clean-template"
        assert jianying_body["draft_manifest"]["package_uri"] == jianying_body["package_artifact"]["uri"]
        assert jianying_body["draft_manifest"]["draft_name"]
        assert jianying_body["draft_manifest"]["tracks_summary"]["main_video"] >= 1
        assert jianying_body["draft_manifest"]["tracks_summary"]["voice_audio"] == 1
        assert jianying_body["draft_manifest"]["tracks_summary"]["subtitle_segments"] > 0
        package_path = local_object_path(get_object_store(), jianying_body["package_artifact"]["uri"])
        with zipfile.ZipFile(package_path) as archive:
            draft_name = jianying_body["draft_manifest"]["draft_name"]
            content = json.loads(archive.read(f"{draft_name}/draft_content.json").decode("utf-8"))
        tracks = {track["name"]: track for track in content["tracks"]}
        assert {"主视频", "旁白", "字幕"} <= set(tracks)
        assert len(tracks["字幕"]["segments"]) == jianying_body["draft_manifest"]["tracks_summary"]["subtitle_segments"]


def test_pipeline_writes_typed_artifact_payloads_with_frame_quantized_timeline():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(title="Typed artifacts"),
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]

    artifacts = {
        artifact.kind: artifact
        for artifact in repository().artifacts.values()
        if artifact.run_id == run["id"]
    }
    narration = artifacts[ArtifactKind.narration_units].payload
    assert narration["source"] == "estimated"
    assert narration["strict"] is False
    assert all({"unit_id", "start", "end", "confidence"} <= set(unit) for unit in narration["units"])

    tts = artifacts[ArtifactKind.audio_tts]
    assert tts.uri and tts.uri.startswith("local://")
    assert tts.sha256 and tts.sha256 != "dev-unpinned"
    assert tts.media_info is not None
    assert tts.media_info.media_type == "audio"
    assert tts.media_info.sample_rate == 16000
    assert tts.media_info.channels == 1
    assert probe_media(local_object_path(get_object_store(), tts.uri)).duration_sec == tts.media_info.duration_sec

    portrait_track = artifacts[ArtifactKind.video_portrait_track]
    assert portrait_track.uri and portrait_track.uri.startswith("local://")
    assert portrait_track.sha256 and portrait_track.sha256 != "dev-unpinned"
    assert portrait_track.media_info is not None
    assert portrait_track.media_info.media_type == "video"
    assert portrait_track.media_info.width == 1080
    assert portrait_track.media_info.height == 1920
    assert get_object_store().exists(parse_object_uri(portrait_track.uri)) is False

    lipsync = artifacts[ArtifactKind.video_lipsync]
    assert lipsync.uri == portrait_track.uri
    assert lipsync.sha256 == portrait_track.sha256
    lipsync_report = artifacts[ArtifactKind.lipsync_report].payload
    assert lipsync_report["skipped"] is True
    assert lipsync_report["input_video_artifact_id"] == portrait_track.id

    timeline = artifacts[ArtifactKind.plan_timeline].payload
    assert timeline["fps"] == 30
    assert timeline["total_frames"] > 0
    assert isinstance(timeline["tracks"], list)
    assert all(isinstance(segment["timeline_start_frame"], int) for segment in timeline["tracks"])
    assert timeline["validation"]["checks"] == {
        "overlap": True,
        "negative_duration": True,
        "out_of_bounds": True,
    }

    rendered = artifacts[ArtifactKind.video_rendered]
    assert rendered.uri and rendered.uri.startswith("local://")
    assert rendered.sha256 and rendered.sha256 != "dev-unpinned"
    assert rendered.media_info is not None
    assert rendered.media_info.width == 1080
    assert rendered.media_info.height == 1920
    assert rendered.media_info.fps == 30
    assert get_object_store().exists(parse_object_uri(rendered.uri)) is False

    final = artifacts[ArtifactKind.video_final]
    assert final.uri and final.uri.startswith("local://")
    assert final.sha256 and final.sha256 != "dev-unpinned"
    assert final.media_info is not None
    assert get_object_store().exists(parse_object_uri(final.uri)) is True
    final_path = local_object_path(get_object_store(), final.uri)
    assert {"video", "audio"} <= probe_stream_types(final_path)
    assert probe_video_frame_count(final_path) == timeline["total_frames"]

    subtitle = artifacts[ArtifactKind.subtitle_ass]
    assert subtitle.uri and subtitle.uri.startswith("local://")
    assert subtitle.sha256 and subtitle.sha256 != "dev-unpinned"
    assert get_object_store().exists(parse_object_uri(subtitle.uri)) is True
    subtitle_text = local_object_path(get_object_store(), subtitle.uri).read_text(encoding="utf-8")
    assert "[Events]" in subtitle_text
    assert "Dialogue:" in subtitle_text

    finished_artifact = artifacts[ArtifactKind.video_finished]
    assert finished_artifact.uri == final.uri
    assert finished_artifact.sha256 == final.sha256
    assert finished_artifact.media_info == final.media_info
    cover = artifacts[ArtifactKind.cover_image]
    assert cover.uri and cover.uri.startswith("local://")
    assert cover.sha256 and cover.sha256 != "dev-unpinned"
    assert cover.media_info is not None
    assert cover.media_info.media_type == "image"
    assert get_object_store().exists(parse_object_uri(cover.uri)) is True
    finished_video = next(
        video for video in repository().finished_videos.values() if video.run_id == run["id"]
    )
    assert finished_video.duration_sec == final.media_info.duration_sec


def test_strict_alignment_rejects_estimated_narration_units():
    login_admin()
    response = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(title="Strict timestamps", strictness={"strict_timestamps": True}),
    )
    assert response.status_code == 201, response.text
    run = response.json()["initial_run"]
    assert run["status"] == "failed"
    detail = client.get(f"/api/runs/{run['id']}").json()
    errors = [node.get("error") for node in detail["node_runs"] if node.get("error")]
    assert errors[-1]["code"] == "render.invalid_timeline"


def test_resume_from_successful_run_reuses_prefix_and_keeps_report_readable():
    login_admin()
    created = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(title="Resume source"),
    )
    assert created.status_code == 201, created.text
    source_run = created.json()["initial_run"]
    assert source_run["status"] == "succeeded"

    resumed = client.post(
        f"/api/runs/{source_run['id']}/resume",
        json={"reason": "reuse successful prefix", "reuse_valid_artifacts": True},
    )

    assert resumed.status_code == 201, resumed.text
    new_run = resumed.json()["run"]
    assert new_run["status"] == "succeeded"
    detail = client.get(f"/api/runs/{new_run['id']}").json()
    assert detail["node_runs"]
    skipped = {node["node_id"] for node in detail["node_runs"] if node["status"] == "skipped"}
    assert {
        "ValidateRequest",
        "LoadCaseContext",
        "ResolveCreativeIntent",
        "TTS",
        "MaterialPackPlanning",
        "NarrationAlignment",
    } <= skipped
    assert "PortraitPlanning" not in skipped
    assert "TimelinePlanning" not in skipped
    report = client.get(f"/api/runs/{new_run['id']}/report")
    assert report.status_code == 200, report.text


def test_resume_from_failed_job_is_rejected_by_state_machine():
    login_admin()
    case = client.post("/api/cases", json={"name": "Resume case"}).json()
    failed = client.post(
        "/api/jobs/digital-human-video",
        json=video_payload(case_id=case["id"], title="Resume hard fail"),
    ).json()
    failed_run = failed["initial_run"]
    resumed = client.post(
        f"/api/runs/{failed_run['id']}/resume",
        json={"reason": "verify resume prefix", "reuse_valid_artifacts": True},
    )
    assert resumed.status_code == 400
    assert resumed.json()["error"]["code"] == "workflow.invalid_transition"
