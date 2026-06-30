"""Pin tests for the SQL-backed publishing patch/delete endpoints before the A1
part-2 fold.

Post-PR#72 the SQL publishing repository is always wired, so the in-memory
``else`` branches in ``apps.api.services.publishing`` are dead and the SQL branch
is the only live path. These tests stamp that live path —
patch_publish_package, delete_publish_batch, patch_publish_item,
delete_publish_item (each hit + missing) — so the kept SQL branch is locked
before the dead in-memory dispatch is folded away.

A 字体 (font) upload (no ffprobe, no auto-created MediaAsset) is enough to mint a
real package artifact, keeping these fast and ffmpeg-free.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.main import app
from tests.api._upload_helpers import direct_upload

client = TestClient(app)


def _login_admin() -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _upload_artifact() -> str:
    _prepared, completed = direct_upload(
        client,
        kind="font",
        filename="pkg.ttf",
        content_type="font/ttf",
        body=b"publish package artifact bytes",
        case_id="case_demo",
        metadata={"template_mode": "replace"},
    )
    assert completed is not None and completed.status_code == 200, completed.text
    return completed.json()["artifact"]["artifact_id"]


def _make_package() -> str:
    package = client.post(
        "/api/publish/packages",
        json={"upload_artifact_id": _upload_artifact(), "title": "原标题", "description": ""},
    )
    assert package.status_code == 201, package.text
    return package.json()["id"]


def _make_batch() -> tuple[str, str]:
    package_id = _make_package()
    batch = client.post(
        "/api/publish/batches",
        json={"publish_package_ids": [package_id], "platform_targets": ["douyin"]},
    )
    assert batch.status_code == 201, batch.text
    body = batch.json()
    return body["id"], body["items"][0]["id"]


def test_patch_publish_package_updates_and_404s_missing():
    _login_admin()
    package_id = _make_package()

    patched = client.patch(f"/api/publish/packages/{package_id}", json={"title": "新标题"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["platform_defaults"]["title"] == "新标题"

    missing = client.patch("/api/publish/packages/pkg_missing", json={"title": "x"})
    assert missing.status_code == 404, missing.text


def test_delete_publish_batch_ok_and_404s_missing():
    _login_admin()
    batch_id, _item_id = _make_batch()

    deleted = client.delete(f"/api/publish/batches/{batch_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True
    assert client.get(f"/api/publish/batches/{batch_id}").status_code == 404

    missing = client.delete("/api/publish/batches/batch_missing")
    assert missing.status_code == 404, missing.text


def test_patch_publish_item_updates_and_404s_missing():
    _login_admin()
    _batch_id, item_id = _make_batch()

    patched = client.patch(
        f"/api/publish/items/{item_id}", json={"title": "条目标题", "selected": False}
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["title"] == "条目标题"
    assert body["selected"] is False

    missing = client.patch("/api/publish/items/item_missing", json={"title": "x"})
    assert missing.status_code == 404, missing.text


def test_delete_publish_item_ok_and_404s_missing():
    _login_admin()
    batch_id, item_id = _make_batch()

    deleted = client.delete(f"/api/publish/items/{item_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True
    detail = client.get(f"/api/publish/batches/{batch_id}")
    assert detail.status_code == 200, detail.text
    assert all(item["id"] != item_id for item in detail.json()["items"])

    missing = client.delete("/api/publish/items/item_missing")
    assert missing.status_code == 404, missing.text
