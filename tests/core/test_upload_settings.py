from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.config.settings import UploadSettings, build_settings


def test_upload_settings_defaults():
    s = UploadSettings()
    assert s.presign_ttl_seconds == 900
    assert s.max_size_bytes == 200 * 1024 * 1024
    assert s.multipart_threshold_bytes == 16 * 1024 * 1024
    assert s.part_size_bytes == 8 * 1024 * 1024
    assert s.cors_allowed_origins == ()


def test_build_settings_upload_defaults(monkeypatch):
    for key in (
        "CUTAGENT_UPLOAD_PRESIGN_TTL_SECONDS",
        "CUTAGENT_UPLOAD_CORS_ALLOWED_ORIGINS",
        "CUTAGENT_UPLOAD_MAX_SIZE_BYTES",
        "CUTAGENT_UPLOAD_MULTIPART_THRESHOLD_BYTES",
        "CUTAGENT_UPLOAD_PART_SIZE_BYTES",
    ):
        monkeypatch.delenv(key, raising=False)
    upload = build_settings().upload
    assert upload.presign_ttl_seconds == 900
    assert upload.max_size_bytes == 200 * 1024 * 1024
    assert upload.multipart_threshold_bytes == 16 * 1024 * 1024
    assert upload.part_size_bytes == 8 * 1024 * 1024
    assert "https://app.shuying.cyou" in upload.cors_allowed_origins


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_size_bytes": 200 * 1024 * 1024 + 1},
        {"part_size_bytes": 5 * 1024 * 1024 - 1},
        {"multipart_threshold_bytes": 17, "max_size_bytes": 16},
    ],
)
def test_upload_settings_reject_unsafe_geometry(overrides):
    with pytest.raises(ValidationError):
        UploadSettings(**overrides)
