from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.api.services import asset_annotation
from packages.core import contracts as c
from packages.media.annotation import BgmAnnotationResult


def _login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _asset(kind: str = "bgm") -> c.MediaAssetRecord:
    return c.MediaAssetRecord(
        id="asset_bgm",
        case_id="case_bgm",
        title="Track",
        kind=kind,
        source_artifact_id="art_bgm",
    )


def _bgm_annotation(asset_id: str = "asset_bgm", *, status=c.AnnotationStatus.completed):
    return c.AnnotationV4(
        meta=c.AnnotationMetaV4(
            asset_id=asset_id,
            case_id="case_bgm",
            material_type="bgm",
            duration=60.0,
            annotation_status=status,
        ),
        bgm_usage_windows=[
            c.BgmUsageWindowV4(
                segment_id="win_drop",
                start=12.0,
                end=24.0,
                duration=12.0,
                role=c.BgmSegmentRole.climax,
                drop_anchor_sec=16.0,
                energy=0.86,
                mood="燃",
                scene_fit=["转场"],
                reason="drop clear",
                source="sensor",
            )
        ],
        quality_report={
            "bgm": {
                "status": "sensor",
                "beats": [12.0, 16.0, 20.0],
                "drops": [16.0],
            }
        },
    )


def test_bgm_rerun_projection_exposes_windows_and_beats(monkeypatch):
    calls: list[dict] = []

    def fake_annotate_bgm(**kwargs):
        calls.append(kwargs)
        return BgmAnnotationResult(
            annotation=_bgm_annotation("asset_bgm_demo"),
            llm_configured=False,
        )

    monkeypatch.setattr(asset_annotation, "annotate_bgm", fake_annotate_bgm)

    with TestClient(create_app()) as client:
        _login_admin(client)

        response = client.post("/api/annotations/asset_bgm_demo/rerun", json={"force": True})

        assert response.status_code == 202, response.text
        editor = client.app.state.repository.annotations["asset_bgm_demo"]
        body = editor.model_dump(mode="json")
        assert body["projection"]["bgm_usage_windows"][0]["segment_id"] == "win_drop"
        assert body["projection"]["bgm"]["beats"] == [12.0, 16.0, 20.0]
        assert "/canonical/bgm_usage_windows" in body["editable_paths"]
        assert calls and calls[0]["audio_profile"] is None


class _FakeProviderRepo:
    def __init__(self) -> None:
        self.capabilities: list[str] = []

    def list_profiles(self, *, capability: str, limit: int):
        self.capabilities.append(capability)
        return []


class _FakeMediaRepo:
    def __init__(self) -> None:
        self.asset = _asset()
        self.persisted: list[dict] = []

    def asset_record(self, asset_id: str) -> c.MediaAssetRecord | None:
        return self.asset if asset_id == self.asset.id else None

    def asset_source_duration(self, asset_id: str) -> float:
        return 60.0

    def media_source_for_asset(self, asset_id: str):
        return ("local://cutagent-local/bgm.mp3", None)

    def persist_annotation_v4(
        self,
        asset_id,
        *,
        canonical,
        projection,
        annotation_status,
        usable,
        case_id=None,
        editable_paths=None,
    ):
        paths = list(editable_paths or ["/labels", "/usable", "/title"])
        self.persisted.append(
            {
                "asset_id": asset_id,
                "canonical": canonical,
                "projection": projection,
                "annotation_status": annotation_status,
                "usable": usable,
                "case_id": case_id,
                "editable_paths": paths,
            }
        )
        return c.AnnotationEditorVm(
            asset=self.asset,
            etag="etag_bgm",
            canonical=canonical,
            projection=projection,
            editable_paths=paths,
        )


def _request() -> SimpleNamespace:
    gateway = SimpleNamespace(get_profile=lambda _pid: None)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(provider_gateway=gateway)))


def test_sqlalchemy_bgm_rerun_uses_audio_understanding_profile_and_urlizer(monkeypatch):
    media_repo = _FakeMediaRepo()
    provider_repo = _FakeProviderRepo()
    calls: list[dict] = []

    monkeypatch.setattr(asset_annotation, "media_repository", lambda _req: media_repo)
    monkeypatch.setattr(asset_annotation, "provider_repository", lambda _req: provider_repo)
    monkeypatch.setattr(asset_annotation, "_sqlalchemy_local_audio_path", lambda *a, **k: None)

    def fake_annotate_bgm(**kwargs):
        calls.append(kwargs)
        return BgmAnnotationResult(annotation=_bgm_annotation(), llm_configured=False)

    monkeypatch.setattr(asset_annotation, "annotate_bgm", fake_annotate_bgm)

    response = asset_annotation.run_sqlalchemy_asset_annotation(
        _request(), media_repo.asset.id, c.RerunAnnotationRequest()
    )

    assert response is not None and response.status == "completed"
    assert provider_repo.capabilities == ["audio.understanding"]
    assert calls and "audio_profile" in calls[0]
    assert "llm_profile" not in calls[0]
    assert callable(calls[0]["audio_url_for_window"])
    assert media_repo.persisted[0]["projection"]["bgm_usage_windows"][0]["segment_id"] == "win_drop"
    assert media_repo.persisted[0]["projection"]["bgm"]["beats"] == [12.0, 16.0, 20.0]
    assert "/canonical/bgm_usage_windows" in media_repo.persisted[0]["editable_paths"]
