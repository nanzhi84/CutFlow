from __future__ import annotations

import json
import zipfile

from fastapi.testclient import TestClient

from apps.api.app import create_app
from packages.core import contracts as c
from packages.core.storage.database import MediaAssetRow
from packages.core.storage.object_store import parse_object_uri
from packages.core.storage.repository import Repository, new_id
from packages.core.storage.sqlalchemy_uploads import artifact_to_row


def test_jianying_draft_endpoint_exports_multitrack_sources_from_plans(media_fixture_factory):
    app = create_app()
    portrait = media_fixture_factory.video(
        duration_sec=2, width=320, height=568, filename="api-portrait.mp4"
    )
    broll = media_fixture_factory.video(
        duration_sec=2, width=320, height=568, filename="api-broll.mp4"
    )
    final_video = media_fixture_factory.video(
        duration_sec=2, width=320, height=568, filename="api-final.mp4"
    )
    voice = media_fixture_factory.audio(duration_sec=2, filename="api-voice.wav")

    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login", json={"email": "admin@local.cutagent", "password": "local-admin"}
        )
        assert login.status_code == 200, login.text

        # Assemble the full run snapshot in a throwaway run-state repo, then flush it
        # into Postgres (the in-memory repo is no longer a storage backend; the SQL
        # jianying exporter reads everything from the database).
        repo = Repository()
        portrait_artifact = _store_artifact(
            app, repo, portrait, kind=c.ArtifactKind.uploaded_file, run_id=None
        )
        broll_artifact = _store_artifact(
            app, repo, broll, kind=c.ArtifactKind.uploaded_file, run_id=None
        )
        final_artifact = _store_artifact(
            app, repo, final_video, kind=c.ArtifactKind.video_final, run_id="run_jy_api"
        )
        voice_artifact = _store_artifact(
            app, repo, voice, kind=c.ArtifactKind.audio_tts, run_id="run_jy_api"
        )
        timeline_artifact = c.Artifact(
            id="art_jy_timeline",
            case_id="case_demo",
            run_id="run_jy_api",
            kind=c.ArtifactKind.plan_timeline,
            payload_schema="TimelinePlanArtifact.v1",
            payload={
                "fps": 30,
                "total_frames": 60,
                "tracks": [
                    {
                        "track_id": "portrait",
                        "segment_id": "portrait_1",
                        "timeline_start_frame": 0,
                        "timeline_end_frame": 60,
                        "source_start_frame": 0,
                        "source_end_frame": 60,
                    },
                    {
                        "track_id": "broll",
                        "segment_id": "broll_1",
                        "timeline_start_frame": 15,
                        "timeline_end_frame": 45,
                        "source_start_frame": 30,
                        "source_end_frame": 60,
                    },
                ],
            },
        )
        portrait_plan = c.Artifact(
            id="art_jy_portrait_plan",
            case_id="case_demo",
            run_id="run_jy_api",
            kind=c.ArtifactKind.plan_portrait,
            payload_schema="PortraitPlanArtifact.v1",
            payload={
                "fps": 30,
                "duration_sec": 2,
                "segments": [
                    {
                        "segment_id": "portrait_1",
                        "asset_id": "asset_jy_portrait",
                        "clip_id": "clip_portrait",
                    }
                ],
            },
        )
        broll_plan = c.Artifact(
            id="art_jy_broll_plan",
            case_id="case_demo",
            run_id="run_jy_api",
            kind=c.ArtifactKind.plan_broll,
            payload_schema="BrollPlanArtifact.v1",
            payload={
                "enabled": True,
                "overlays": [
                    {"overlay_id": "broll_1", "asset_id": "asset_jy_broll", "clip_id": "clip_broll"}
                ],
            },
        )
        narration = c.Artifact(
            id="art_jy_narration",
            case_id="case_demo",
            run_id="run_jy_api",
            kind=c.ArtifactKind.narration_units,
            payload_schema="NarrationUnitsArtifact.v1",
            payload={"units": [{"text": "测试字幕", "start": 0.0, "end": 1.0}]},
        )
        for artifact in [voice_artifact, timeline_artifact, portrait_plan, broll_plan, narration]:
            repo.artifacts[artifact.id] = artifact

        job = c.Job(
            id="job_jy_api",
            type=c.JobType.digital_human_video,
            case_id="case_demo",
            created_by="usr_admin",
            request_schema="v1",
            request=c.DigitalHumanVideoRequest(
                case_id="case_demo", script="test", voice={"voice_id": "voice_sandbox"}
            ),
        )
        run = c.WorkflowRun(
            id="run_jy_api",
            job_id=job.id,
            case_id="case_demo",
            workflow_template_id="digital-human-video",
            workflow_version="v1",
            status=c.RunStatus.succeeded,
            requested_by="usr_admin",
        )
        finished = c.FinishedVideo(
            id="fv_jy_api",
            case_id="case_demo",
            run_id=run.id,
            owner_user_id="usr_admin",
            title="API 剪映工程",
            video_artifact=repo.artifact_ref(final_artifact.id),
            duration_sec=2.0,
        )
        version = c.VideoVersion(
            id="vv_jy_api",
            case_id="case_demo",
            finished_video_id=finished.id,
            timeline_plan_artifact_id=timeline_artifact.id,
            style_plan_artifact_id="art_missing_style",
        )
        repo.jobs[job.id] = job.model_copy(
            update={"active_run_id": run.id, "latest_finished_video_id": finished.id}
        )
        repo.runs[run.id] = run
        repo.finished_videos[finished.id] = finished
        repo.video_versions[version.id] = version
        repo.artifacts["art_jy_stale_package"] = c.Artifact(
            id="art_jy_stale_package",
            case_id="case_demo",
            run_id="run_jy_api",
            kind=c.ArtifactKind.jianying_draft,
            uri=final_artifact.uri,
            payload_schema="JianyingDraftPackageArtifact.v1",
            payload={
                "finished_video_id": finished.id,
                "draft_name": "旧格式包",
            },
        )

        # Persist the run_id=None uploaded sources + their media-asset rows directly
        # (``sync_workflow_snapshot`` only flushes run-scoped artifacts), then flush the
        # run snapshot (job/run/run-scoped artifacts/finished/video version) to Postgres.
        with app.state.sqlalchemy_session_factory() as session:
            session.merge(artifact_to_row(portrait_artifact))
            session.merge(artifact_to_row(broll_artifact))
            session.flush()
            session.add(
                MediaAssetRow(
                    id="asset_jy_portrait",
                    case_id="case_demo",
                    title="API portrait",
                    kind="video",
                    source_artifact_id=portrait_artifact.id,
                    annotation_status="annotated",
                    usable=True,
                )
            )
            session.add(
                MediaAssetRow(
                    id="asset_jy_broll",
                    case_id="case_demo",
                    title="API broll",
                    kind="video",
                    source_artifact_id=broll_artifact.id,
                    annotation_status="annotated",
                    usable=True,
                )
            )
            session.commit()
        app.state.sqlalchemy_production_repository.sync_workflow_snapshot(
            job=repo.jobs[job.id], run=run, repository=repo
        )

        missing_latest = client.get(f"/api/finished-videos/{finished.id}/jianying-draft/latest")
        assert missing_latest.status_code == 200, missing_latest.text
        assert missing_latest.json()["package"] is None

        response = client.post(f"/api/finished-videos/{finished.id}/jianying-draft", json={})
        assert response.status_code == 201, response.text

        latest = client.get(f"/api/finished-videos/{finished.id}/jianying-draft/latest")
        assert latest.status_code == 200, latest.text
        latest_body = latest.json()
        assert latest_body["package"]["package_artifact"]["artifact_id"] == response.json()["package_artifact"]["artifact_id"]
        assert latest_body["package"]["download_url"].startswith(("http://", "https://", "/"))

        body = response.json()
        assert body["draft_manifest"]["portable_resources"] is True
        assert body["download_url"].startswith(("http://", "https://", "/"))
        assert body["download_expires_at"]
        if body["download_url"].startswith("/"):
            download = client.get(body["download_url"])
            assert download.status_code == 200, download.text
            assert download.headers["content-type"] == "application/zip"
            assert download.content.startswith(b"PK")

    manifest = body["draft_manifest"]
    package_path = app.state.object_store._path(parse_object_uri(manifest["package_uri"]))
    with zipfile.ZipFile(package_path) as archive:
        names = set(archive.namelist())
        content = json.loads(
            archive.read(f"{manifest['draft_name']}/draft_content.json").decode("utf-8")
        )

    assert not any(name.endswith("api-final.mp4") for name in names)
    tracks = {track["name"]: track for track in content["tracks"]}
    assert {"主视频", "B-roll覆盖", "旁白", "字幕"}.issubset(tracks)
    video_materials = {material["id"]: material for material in content["materials"]["videos"]}
    assert (
        video_materials[tracks["主视频"]["segments"][0]["material_id"]]["material_name"]
        == "api-portrait.mp4"
    )
    assert (
        video_materials[tracks["B-roll覆盖"]["segments"][0]["material_id"]]["material_name"]
        == "api-broll.mp4"
    )
    assert tracks["B-roll覆盖"]["segments"][0]["target_timerange"] == {
        "start": 500_000,
        "duration": 1_000_000,
    }
    assert tracks["B-roll覆盖"]["segments"][0]["source_timerange"] == {
        "start": 1_000_000,
        "duration": 1_000_000,
    }
    assert content["materials"]["audios"][0]["name"] == "api-voice.wav"


def _store_artifact(
    app, repo: Repository, path, *, kind: c.ArtifactKind, run_id: str | None
) -> c.Artifact:
    ref = app.state.object_store.prepare_upload(path.name, "test-jianying")
    stored = app.state.object_store.put_bytes(ref, path.read_bytes())
    artifact = c.Artifact(
        id=new_id("art"),
        case_id="case_demo",
        run_id=run_id,
        kind=kind,
        uri=stored.ref.uri,
        payload_schema=f"{kind.value}.v1",
        payload={},
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
    )
    repo.artifacts[artifact.id] = artifact
    return artifact
