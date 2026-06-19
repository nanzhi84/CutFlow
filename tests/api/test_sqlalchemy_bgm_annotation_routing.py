"""Routing guard for the production (SQLAlchemy) asset-annotation rerun.

Regression test for the bug where ``run_sqlalchemy_asset_annotation`` unconditionally
ran the VISUAL ``annotate_asset`` path even for ``kind='bgm'`` assets -- clobbering a
real librosa+LLM BGM annotation with a degraded/empty visual AnnotationV4.

These run fully offline: the media/provider repos are lightweight fakes, the local
source resolver is stubbed, and both ``annotate_bgm`` / ``annotate_asset`` are replaced
with spies, so no ffmpeg / librosa / network / DB is touched. We only assert ROUTING
(which annotation entry point is invoked) + that the BGM canonical/projection carry the
``quality_report.bgm`` written by the audio path.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api.services import asset_annotation
from packages.core import contracts as c
from packages.media.annotation import BgmAnnotationResult, GatedAnnotationResult


def _asset(kind: str) -> c.MediaAssetRecord:
    return c.MediaAssetRecord(
        id="asset_x",
        case_id="case_demo",
        title="track" if kind == "bgm" else "clip",
        kind=kind,
        source_artifact_id="art_src",
    )


def _bgm_annotation(*, status: c.AnnotationStatus, with_mood: bool) -> c.AnnotationV4:
    bgm_report: dict = {"status": "ok" if with_mood else "failed", "bpm": 128.0, "source": "librosa+llm"}
    if with_mood:
        bgm_report.update({"mood": "upbeat", "genre": "edm"})
    return c.AnnotationV4(
        meta=c.AnnotationMetaV4(
            asset_id="asset_x",
            case_id="case_demo",
            material_type="bgm",
            duration=90.0,
            annotation_status=status,
        ),
        quality_report={"bgm": bgm_report},
    )


def _visual_annotation() -> c.AnnotationV4:
    return c.AnnotationV4(
        meta=c.AnnotationMetaV4(
            asset_id="asset_x",
            case_id="case_demo",
            material_type="broll",
            duration=12.0,
            annotation_status=c.AnnotationStatus.completed,
        ),
        quality_report={"vlm_status": "vlm_unconfigured"},
    )


class _FakeMediaRepo:
    def __init__(self, asset: c.MediaAssetRecord) -> None:
        self._asset = asset
        self.persisted: list[dict] = []

    def asset_record(self, asset_id: str) -> c.MediaAssetRecord | None:
        return self._asset if asset_id == self._asset.id else None

    def asset_source_duration(self, asset_id: str) -> float:
        return 90.0

    def media_source_for_asset(self, asset_id: str):  # pragma: no cover - resolver is stubbed
        return ("s3://bucket/bgm.mp3", None)

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
        self.persisted.append(
            {
                "asset_id": asset_id,
                "canonical": canonical,
                "projection": projection,
                "annotation_status": annotation_status,
                "usable": usable,
                "case_id": case_id,
                "editable_paths": list(editable_paths or ["/labels", "/usable", "/title"]),
            }
        )
        return c.AnnotationEditorVm(
            asset=self._asset,
            etag="etag_1",
            canonical=canonical,
            projection=projection,
            editable_paths=list(editable_paths or ["/labels", "/usable", "/title"]),
        )


def _request() -> SimpleNamespace:
    gateway = SimpleNamespace(get_profile=lambda _pid: None)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(provider_gateway=gateway)))


def _wire(monkeypatch, asset: c.MediaAssetRecord) -> tuple[_FakeMediaRepo, dict]:
    media_repo = _FakeMediaRepo(asset)
    calls: dict[str, list] = {"bgm": [], "visual": []}

    monkeypatch.setattr(asset_annotation, "media_repository", lambda _req: media_repo)
    monkeypatch.setattr(asset_annotation, "provider_repository", lambda _req: None)
    # The s3:// -> local-path resolution is exercised elsewhere; stub it here so the
    # test stays offline regardless of object-store wiring.
    monkeypatch.setattr(asset_annotation, "_sqlalchemy_local_audio_path", lambda *a, **k: "/tmp/bgm.mp3")
    monkeypatch.setattr(asset_annotation, "_sqlalchemy_local_video_path", lambda *a, **k: "/tmp/clip.mp4")

    def fake_annotate_bgm(**kwargs):
        calls["bgm"].append(kwargs)
        return BgmAnnotationResult(
            annotation=_bgm_annotation(status=c.AnnotationStatus.completed, with_mood=True),
            llm_configured=True,
        )

    def fake_annotate_asset(**kwargs):
        calls["visual"].append(kwargs)
        return GatedAnnotationResult(
            annotation=_visual_annotation(), vlm_configured=False, provider_invocation_ids=[]
        )

    monkeypatch.setattr(asset_annotation, "annotate_bgm", fake_annotate_bgm)
    monkeypatch.setattr(asset_annotation, "annotate_asset", fake_annotate_asset)
    return media_repo, calls


def test_sqlalchemy_bgm_asset_routes_to_bgm_path(monkeypatch):
    media_repo, calls = _wire(monkeypatch, _asset("bgm"))

    response = asset_annotation.run_sqlalchemy_asset_annotation(
        _request(), "asset_x", c.RerunAnnotationRequest()
    )

    # Routed to the audio path, NOT the visual VLM path.
    assert len(calls["bgm"]) == 1
    assert calls["visual"] == []
    # annotate_bgm received the BGM-shaped args (audio_path/asset_title), not video_path.
    bgm_kwargs = calls["bgm"][0]
    assert "audio_path" in bgm_kwargs and "video_path" not in bgm_kwargs
    assert bgm_kwargs["asset_title"] == "track"

    # Persisted via the SAME repository writer, carrying the bgm quality_report.
    assert len(media_repo.persisted) == 1
    persisted = media_repo.persisted[0]
    assert persisted["canonical"]["quality_report"]["bgm"]["mood"] == "upbeat"
    assert persisted["projection"]["bgm"]["genre"] == "edm"
    assert persisted["annotation_status"] == "annotated"
    assert persisted["usable"] is True
    assert response is not None and response.status == "completed"


def test_sqlalchemy_non_bgm_asset_still_uses_visual_path(monkeypatch):
    _media_repo, calls = _wire(monkeypatch, _asset("broll"))

    response = asset_annotation.run_sqlalchemy_asset_annotation(
        _request(), "asset_x", c.RerunAnnotationRequest()
    )

    # Visual path runs; the BGM path is never touched for a non-audio asset.
    assert len(calls["visual"]) == 1
    assert calls["bgm"] == []
    assert response is not None and response.status == "completed"


def test_sqlalchemy_bgm_unconfigured_degrades_without_crash(monkeypatch):
    """No real llm.chat profile -> degraded BGM annotation persisted (not a crash)."""
    media_repo = _FakeMediaRepo(_asset("bgm"))
    monkeypatch.setattr(asset_annotation, "media_repository", lambda _req: media_repo)
    monkeypatch.setattr(asset_annotation, "provider_repository", lambda _req: None)
    monkeypatch.setattr(asset_annotation, "_sqlalchemy_local_audio_path", lambda *a, **k: "")

    visual_calls: list = []
    monkeypatch.setattr(
        asset_annotation,
        "annotate_asset",
        lambda **k: visual_calls.append(k),
    )

    def degraded_bgm(**kwargs):
        # Mirror annotate_bgm's degrade contract: llm_configured False, status failed.
        return BgmAnnotationResult(
            annotation=_bgm_annotation(status=c.AnnotationStatus.failed, with_mood=False),
            llm_configured=False,
        )

    monkeypatch.setattr(asset_annotation, "annotate_bgm", degraded_bgm)

    response = asset_annotation.run_sqlalchemy_asset_annotation(
        _request(), "asset_x", c.RerunAnnotationRequest()
    )

    # Degraded llm-unconfigured run is "completed" (a degraded run, not a failed paid call),
    # never falls through to the visual path, and persists a not-usable annotation.
    assert visual_calls == []
    assert len(media_repo.persisted) == 1
    assert media_repo.persisted[0]["usable"] is False
    assert media_repo.persisted[0]["annotation_status"] == "annotation_failed"
    assert response is not None and response.status == "completed"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
