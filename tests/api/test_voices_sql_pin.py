"""Pin tests for the SQL-backed voices endpoints before the A1 part-2 fold.

Post-PR#72 the SQL media repository is always wired, so the in-memory
``else`` branches in ``apps.api.services.voices`` are dead and the SQL branch is
the only live path. These tests stamp that live path — list_voices
source/vendor/enabled filtering, patch_voice (hit + missing), delete_voice — so
the kept SQL branch is locked before the dead in-memory dispatch is folded away.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.main import app
from packages.core.storage.database import VoiceProfileRow
from packages.core.storage.repository import new_id

client = TestClient(app)


def _login_admin() -> None:
    response = client.post(
        "/api/auth/login",
        json={"email": "admin@local.cutagent", "password": "local-admin"},
    )
    assert response.status_code == 200, response.text


def _insert_voice(*, source: str, vendor: str, enabled: bool, display_name: str) -> str:
    voice_id = new_id("voice")
    with app.state.sqlalchemy_session_factory() as session:
        session.add(
            VoiceProfileRow(
                id=voice_id,
                display_name=display_name,
                source=source,
                vendor=vendor,
                provider_profile_id="sandbox.tts.default",
                enabled=enabled,
            )
        )
        session.commit()
    return voice_id


def test_list_voices_filters_by_source_vendor_enabled():
    _login_admin()
    cloned_id = _insert_voice(source="cloned", vendor="pintest_volc", enabled=True, display_name="A")
    builtin_id = _insert_voice(source="builtin", vendor="pintest_mmx", enabled=False, display_name="B")

    by_source = client.get("/api/voices", params={"source": "cloned"})
    assert by_source.status_code == 200, by_source.text
    source_items = by_source.json()["items"]
    assert any(v["id"] == cloned_id for v in source_items)
    assert all(v["source"] == "cloned" for v in source_items)

    by_vendor = client.get("/api/voices", params={"vendor": "pintest_mmx"})
    assert by_vendor.status_code == 200, by_vendor.text
    assert [v["id"] for v in by_vendor.json()["items"]] == [builtin_id]

    by_enabled = client.get("/api/voices", params={"enabled": False})
    assert by_enabled.status_code == 200, by_enabled.text
    disabled_items = by_enabled.json()["items"]
    assert any(v["id"] == builtin_id for v in disabled_items)
    assert all(v["enabled"] is False for v in disabled_items)


def test_patch_voice_updates_existing_and_rejects_missing():
    _login_admin()
    voice_id = _insert_voice(source="cloned", vendor="pintest_volc", enabled=True, display_name="Before")

    patched = client.patch(
        f"/api/voices/{voice_id}", json={"display_name": "After", "enabled": False}
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["display_name"] == "After"
    assert body["enabled"] is False

    missing = client.patch("/api/voices/voice_does_not_exist", json={"display_name": "x"})
    assert missing.status_code == 400, missing.text
    assert missing.json()["error"]["code"] == "validation.missing_voice"


def test_delete_voice_returns_ok():
    _login_admin()
    voice_id = _insert_voice(source="cloned", vendor="pintest_volc", enabled=True, display_name="Del")

    deleted = client.delete(f"/api/voices/{voice_id}")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["ok"] is True

    # Deleting an unknown voice is idempotent: still an OkResponse, no 404.
    again = client.delete(f"/api/voices/{voice_id}")
    assert again.status_code == 200, again.text
    assert again.json()["ok"] is True

    listed = client.get("/api/voices", params={"vendor": "pintest_volc"})
    assert all(v["id"] != voice_id for v in listed.json()["items"])
