import hashlib

from fastapi.testclient import TestClient

from apps.api.main import app, repository
from tests.fixtures.media import generate_test_video

client = TestClient(app)


def login_admin() -> None:
    response = client.post("/api/auth/login", json={"email": "admin@local.cutagent", "password": "local-admin"})
    assert response.status_code == 200, response.text


def upload_video(
    tmp_path,
    *,
    filename: str,
    case_id: str,
    title: str | None = None,
    replace_mode: bool = False,
    duration_sec: float = 1,
) -> dict:
    video = generate_test_video(tmp_path, duration_sec=duration_sec, width=160, height=120, fps=15, filename=filename)
    content = video.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "broll",
            "case_id": case_id,
            "filename": filename,
            "content_type": "video/mp4",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(f"/api/uploads/{upload['id']}/file", files={"file": (filename, content, "video/mp4")})
    assert uploaded.status_code == 200, uploaded.text
    metadata = {"title": title or filename}
    if replace_mode:
        metadata["template_mode"] = "replace"
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest, "metadata": metadata},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


def test_single_replace_source_preserves_existing_annotation(tmp_path):
    login_admin()
    original = upload_video(tmp_path, filename="single-original.mp4", case_id="case_single_replace")
    asset_id = original["media_asset"]["id"]
    editor = client.get(f"/api/annotations/{asset_id}").json()
    # Per Spec §12.2, an edited segment is validated as a ClipV4 and merged into the
    # canonical AnnotationV4 (not a free-JSON projection blob). The structural edit lands
    # in canonical.clips, which material planning consumes via annotation_v4_for_asset.
    kept_segment = {
        "segment_id": "seg_keep",
        "start": 0.0,
        "end": 0.5,
        "duration": 0.5,
        "usage": {"role": "cover", "recommended_for_voiceover": True},
        "retrieval": {"summary": "keep"},
    }
    patched = client.patch(
        f"/api/annotations/{asset_id}",
        json={
            "etag": editor["etag"],
            "patch": {"operations": [{"op": "replace", "path": "/canonical/segments", "value": [kept_segment]}]},
        },
    )
    assert patched.status_code == 200, patched.text
    replacement = upload_video(
        tmp_path, filename="single-replacement.mp4", case_id="case_single_replace", replace_mode=True
    )
    assert replacement["media_asset"] is None

    response = client.post(
        f"/api/media/assets/{asset_id}/replace-source",
        json={"upload_session_id": replacement["upload_session"]["id"]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["asset"]["id"] == asset_id
    assert body["artifact"]["artifact_id"] == replacement["artifact"]["artifact_id"]
    assert body["preserved_annotation"] is True
    # The edited segment is now owned by the canonical AnnotationV4 (clips), not a blob.
    canonical = repository().annotations[asset_id].canonical
    assert [c["segment_id"] for c in canonical["clips"]] == ["seg_keep"]
    assert canonical["clips"][0]["usage"]["role"] == "cover"


def test_replace_source_reclips_annotation_on_duration_drift(tmp_path):
    """Replacing with a shorter clip re-clips the preserved annotation to the new duration."""
    login_admin()
    original = upload_video(
        tmp_path, filename="drift-original.mp4", case_id="case_drift", duration_sec=2
    )
    asset_id = original["media_asset"]["id"]
    editor = client.get(f"/api/annotations/{asset_id}").json()
    # A clip that ends at the OLD end-of-video (2s); after re-clip to 1s it must clamp.
    seg = {
        "segment_id": "seg_full",
        "start": 0.0,
        "end": 2.0,
        "duration": 2.0,
        "usage": {"role": "cover", "recommended_for_voiceover": True},
    }
    patched = client.patch(
        f"/api/annotations/{asset_id}",
        json={"etag": editor["etag"], "patch": {"operations": [{"op": "replace", "path": "/canonical/segments", "value": [seg]}]}},
    )
    assert patched.status_code == 200, patched.text

    replacement = upload_video(
        tmp_path, filename="drift-replacement.mp4", case_id="case_drift", replace_mode=True, duration_sec=1
    )
    response = client.post(
        f"/api/media/assets/{asset_id}/replace-source",
        json={"upload_session_id": replacement["upload_session"]["id"]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # Annotation preserved, but re-clipped: the clip is clamped to the new 1s duration.
    assert body["preserved_annotation"] is True
    canonical = repository().annotations[asset_id].canonical
    assert canonical["clips"][0]["end"] <= 1.0 + 1e-6
    assert canonical["meta"]["duration"] <= 1.0 + 1e-6


def test_patch_annotation_rejects_invalid_segment_schema(tmp_path):
    """A free-JSON segment (missing start/end, unknown fields) is rejected per §2.3."""
    login_admin()
    original = upload_video(tmp_path, filename="invalid-seg.mp4", case_id="case_invalid_seg")
    asset_id = original["media_asset"]["id"]
    editor = client.get(f"/api/annotations/{asset_id}").json()
    response = client.patch(
        f"/api/annotations/{asset_id}",
        json={
            "etag": editor["etag"],
            "patch": {"operations": [{"op": "replace", "path": "/canonical/segments", "value": [{"label": "keep"}]}]},
        },
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "artifact.schema_mismatch"


def test_auto_match_replace_reports_matched_unmatched_and_ambiguous(tmp_path):
    login_admin()
    matched = upload_video(tmp_path, filename="Hero Clip.mp4", case_id="case_auto_replace", title="Hero Clip")
    upload_video(tmp_path, filename="Duplicate A.mp4", case_id="case_auto_replace", title="Duplicate")
    upload_video(tmp_path, filename="Duplicate B.mp4", case_id="case_auto_replace", title="Duplicate")
    matched_asset_id = matched["media_asset"]["id"]
    replacement = upload_video(tmp_path, filename="hero-clip.mp4", case_id="case_auto_replace", replace_mode=True)
    unmatched = upload_video(tmp_path, filename="missing.mp4", case_id="case_auto_replace", replace_mode=True)
    ambiguous = upload_video(tmp_path, filename="duplicate.mp4", case_id="case_auto_replace", replace_mode=True)

    response = client.post(
        "/api/media/assets/auto-match-replace",
        json={
            "case_id": "case_auto_replace",
            "kind": "broll",
            "upload_session_ids": [
                replacement["upload_session"]["id"],
                unmatched["upload_session"]["id"],
                ambiguous["upload_session"]["id"],
            ],
        },
    )

    assert response.status_code == 200, response.text
    results = {item["filename"]: item for item in response.json()["results"]}
    assert results["hero-clip.mp4"]["status"] == "matched"
    assert results["hero-clip.mp4"]["asset_id"] == matched_asset_id
    assert results["missing.mp4"]["status"] == "unmatched"
    assert results["duplicate.mp4"]["status"] == "ambiguous"
    assert repository().media_assets[matched_asset_id].source_artifact_id == replacement["artifact"]["artifact_id"]


def upload_cover_template(tmp_path, *, filename: str, case_id: str, title: str) -> dict:
    image = tmp_path / filename
    # A tiny valid PNG is enough; the upload path only needs real bytes + sha.
    image.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f"
            "15c4890000000b49444154789c6360000200000500017a5eab3f00000000"
            "49454e44ae426082"
        )
    )
    content = image.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    prepared = client.post(
        "/api/uploads/prepare",
        json={
            "kind": "cover_template",
            "case_id": case_id,
            "filename": filename,
            "content_type": "image/png",
            "size_bytes": len(content),
            "sha256": digest,
        },
    )
    assert prepared.status_code == 201, prepared.text
    upload = prepared.json()
    uploaded = client.put(f"/api/uploads/{upload['id']}/file", files={"file": (filename, content, "image/png")})
    assert uploaded.status_code == 200, uploaded.text
    completed = client.post(
        "/api/uploads/complete",
        json={"upload_session_id": upload["id"], "size_bytes": len(content), "sha256": digest, "metadata": {"title": title}},
    )
    assert completed.status_code == 200, completed.text
    body = completed.json()
    if body.get("media_asset"):
        return body["media_asset"]
    created = client.post(
        "/api/media/assets",
        json={
            "upload_session_id": upload["id"],
            "case_id": case_id,
            "title": title,
            "kind": "cover_template",
        },
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_delete_cover_template_asset_removes_it_from_listing(tmp_path):
    login_admin()
    asset = upload_cover_template(
        tmp_path, filename="style-ref.png", case_id="case_cover_tpl", title="风格参考"
    )
    asset_id = asset["id"]

    listed = client.get("/api/media/assets", params={"case_id": "case_cover_tpl", "kind": "cover_template"})
    assert listed.status_code == 200, listed.text
    assert any(card["asset"]["id"] == asset_id for card in listed.json()["items"])

    deleted = client.delete(f"/api/media/assets/{asset_id}")
    assert deleted.status_code == 200, deleted.text

    after = client.get("/api/media/assets", params={"case_id": "case_cover_tpl", "kind": "cover_template"})
    assert not any(card["asset"]["id"] == asset_id for card in after.json()["items"])

    # Deleting again is a 4xx (already gone), not a silent 200.
    missing = client.delete(f"/api/media/assets/{asset_id}")
    assert missing.status_code >= 400
