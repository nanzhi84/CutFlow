from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.contracts import PrepareUploadRequest, PrepareUploadResponse
from packages.core.contracts.media import ALLOWED_UPLOAD_CONTENT_TYPES, UploadKind

# 100 MiB is a deliberate product hard cap, NOT a bug to "fix" by lifting it.
# #87 C6 is wontfix-by-design: uploads are a single browser-direct presigned PUT
# (no chunked/resumable transfer), so a single PUT must reliably complete within
# this bound. Reviving multipart/resume would force a contract change (this `le`),
# an object_store multipart protocol, complete()'s sha256 re-hash off the API
# process, an alembic part/uploadId migration, and a frontend upload queue —
# tracked as too-expensive in #87. This test pins the cap so it isn't quietly raised.
_100_MIB = 100 * 1024 * 1024


def test_size_cap_enforced():
    """Pin the #87 C6 wontfix product cap: exactly 100 MiB is accepted, one byte
    over is rejected. Do not relax this without re-opening the chunked-upload design."""
    PrepareUploadRequest(
        kind=UploadKind.publish_video, filename="v.mp4",
        content_type="video/mp4", size_bytes=_100_MIB,
    )
    with pytest.raises(ValidationError):
        PrepareUploadRequest(
            kind=UploadKind.publish_video, filename="v.mp4",
            content_type="video/mp4", size_bytes=_100_MIB + 1,
        )


def test_multipart_field_removed():
    assert "multipart" not in PrepareUploadRequest.model_fields


def test_allowlist_covers_every_kind():
    assert set(ALLOWED_UPLOAD_CONTENT_TYPES) == set(UploadKind)
    assert "video/mp4" in ALLOWED_UPLOAD_CONTENT_TYPES[UploadKind.publish_video]
    assert "audio/mpeg" in ALLOWED_UPLOAD_CONTENT_TYPES[UploadKind.voice_reference]
    assert "font/ttf" in ALLOWED_UPLOAD_CONTENT_TYPES[UploadKind.font]


def test_prepare_response_shape():
    fields = set(PrepareUploadResponse.model_fields)
    assert {"upload_session", "put_url", "put_content_type", "expires_at"} <= fields
