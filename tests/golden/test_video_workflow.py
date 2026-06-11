from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.main import app
from apps.api.main import repository
from packages.ai.gateway.provider_gateway import ProviderRuntimeError, SandboxProvider
from packages.core.contracts import ArtifactKind
from packages.core.contracts import ErrorCode


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


def test_spec_20_2_2_broll_enabled_success_creates_non_empty_plan():
    """Spec 20.2 #2: B-roll enabled success."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
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


def test_spec_20_2_8_timeline_out_of_bounds_is_rejected():
    """Spec 20.2 #8 / spec 2.3: out-of-bounds timeline segment hard-fails."""
    with fresh_client() as active_client:
        login_admin_for(active_client)
        response = active_client.post(
            "/api/jobs/digital-human-video",
            json=video_payload(
                title="Timeline out of bounds",
                script="短",
                broll={"enabled": True, "max_inserts": 1},
            ),
        )
        assert response.status_code == 201, response.text
        run = response.json()["initial_run"]
        assert run["status"] == "failed"
        errors = node_errors(active_client, run["id"])
        assert errors[-1]["code"] == "render.invalid_timeline"
        report = run_report(active_client, run["id"])
        assert report["debug_report"]["node_errors"][-1]["code"] == "render.invalid_timeline"


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
        assert handoff.json()["package_artifact"]["kind"] == "editor.handoff_package"
        assert handoff.json()["manifest"]["finished_video_id"] == finished_video_id

        jianying = active_client.post(
            f"/api/finished-videos/{finished_video_id}/jianying-draft",
            json={"template_id": "clean-template"},
        )
        assert jianying.status_code == 201, jianying.text
        assert jianying.json()["package_artifact"]["kind"] == "editor.jianying_draft_package"
        assert jianying.json()["draft_manifest"]["template_id"] == "clean-template"


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

    timeline = artifacts[ArtifactKind.timeline_plan].payload
    assert timeline["fps"] == 30
    assert timeline["total_frames"] > 0
    assert isinstance(timeline["tracks"], list)
    assert all(isinstance(segment["timeline_start_frame"], int) for segment in timeline["tracks"])
    assert timeline["validation"]["checks"] == {
        "overlap": True,
        "negative_duration": True,
        "out_of_bounds": True,
    }


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
    assert all(node["status"] == "skipped" for node in detail["node_runs"])
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
