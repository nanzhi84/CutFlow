from __future__ import annotations

import pytest
from pydantic import ValidationError

from packages.core.contracts import (
    ObjectCompleteUploadRequest,
    ObjectCompleteUploadResponse,
    PrepareUploadRequest,
    PrepareUploadResponse,
    ResumeUploadResponse,
    SignUploadPartsRequest,
    SignUploadPartsResponse,
)
from packages.core.contracts.media import ALLOWED_UPLOAD_CONTENT_TYPES, UploadKind

# Issue #210 raises the hard product cap to exactly 200 MiB and routes files at
# or above 16 MiB through the resumable multipart protocol.
_200_MIB = 200 * 1024 * 1024


def test_size_cap_enforced():
    """Exactly 200 MiB is accepted and one byte over is rejected."""
    PrepareUploadRequest(
        client_upload_id="client_contract_200mib",
        kind=UploadKind.publish_video,
        filename="v.mp4",
        content_type="video/mp4",
        size_bytes=_200_MIB,
    )
    with pytest.raises(ValidationError):
        PrepareUploadRequest(
            client_upload_id="client_contract_too_large",
            kind=UploadKind.publish_video,
            filename="v.mp4",
            content_type="video/mp4",
            size_bytes=_200_MIB + 1,
        )


def test_stable_client_upload_id_is_required():
    with pytest.raises(ValidationError):
        PrepareUploadRequest(
            kind=UploadKind.video,
            filename="v.mp4",
            content_type="video/mp4",
            size_bytes=1,
        )


def test_allowlist_covers_every_kind():
    assert set(ALLOWED_UPLOAD_CONTENT_TYPES) == set(UploadKind)
    assert "video/mp4" in ALLOWED_UPLOAD_CONTENT_TYPES[UploadKind.publish_video]
    assert "audio/mpeg" in ALLOWED_UPLOAD_CONTENT_TYPES[UploadKind.voice_reference]
    assert "font/ttf" in ALLOWED_UPLOAD_CONTENT_TYPES[UploadKind.font]


def test_prepare_response_shape():
    fields = set(PrepareUploadResponse.model_fields)
    assert {
        "upload_session",
        "upload_strategy",
        "part_size_bytes",
        "part_count",
        "put_url",
        "put_content_type",
        "expires_at",
    } <= fields


def test_resumable_request_and_response_shapes():
    assert {"part_numbers"} == set(SignUploadPartsRequest.model_fields)
    assert {"upload_session", "parts"} <= set(SignUploadPartsResponse.model_fields)
    assert {"size_bytes", "sha256", "metadata"} == set(ObjectCompleteUploadRequest.model_fields)
    assert {
        "upload_session",
        "artifact",
        "media_asset",
        "publish_package",
        "request_id",
    } <= set(ObjectCompleteUploadResponse.model_fields)
    assert {"completed_parts", "artifact", "request_id"} <= set(ResumeUploadResponse.model_fields)

    with pytest.raises(ValidationError):
        SignUploadPartsRequest(part_numbers=[1, 1])
    with pytest.raises(ValidationError):
        SignUploadPartsRequest(part_numbers=[0])
