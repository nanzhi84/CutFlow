"""Gated end-to-end multipart recovery against a real S3-compatible MinIO."""

from __future__ import annotations

import hashlib
import os
import urllib.request
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

if os.getenv("CUTAGENT_RUN_S3_TESTS") != "1":
    pytest.skip(
        "Set CUTAGENT_RUN_S3_TESTS=1 to run MinIO multipart recovery tests.",
        allow_module_level=True,
    )

from starlette.requests import Request

from apps.api.services import uploads as upload_service
from packages.core import contracts as c
from packages.core.config.settings import UploadSettings
from packages.core.storage.object_store import S3ObjectStore
from packages.core.storage.repository import new_id
from packages.core.storage.sqlalchemy_uploads import SqlAlchemyUploadRepository
from packages.media.upload_reconciler import UploadReconciler
from tests.api._upload_helpers import minimal_ttf_bytes

MIB = 1024 * 1024
PART_SIZE = 8 * MIB


def _store(tmp_path, bucket: str, cache_name: str) -> S3ObjectStore:
    return S3ObjectStore(
        endpoint_url=os.getenv("CUTAGENT_OBJECTSTORE_ENDPOINT", "http://127.0.0.1:9000"),
        bucket=bucket,
        access_key=os.getenv("CUTAGENT_OBJECTSTORE_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("CUTAGENT_OBJECTSTORE_SECRET_KEY", "minioadmin"),
        addressing_style="path",
        cache_root=tmp_path / cache_name,
    )


def _put_part(
    store: S3ObjectStore,
    uri: str,
    upload_id: str,
    part_number: int,
    content: bytes,
) -> None:
    signed = store.sign_upload_part(
        uri,
        upload_id=upload_id,
        part_number=part_number,
        expires_in=timedelta(minutes=5),
    )
    request = urllib.request.Request(signed.url, data=content, method="PUT")
    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 200
        assert response.headers.get("ETag")


def _incomplete_session(
    repository: SqlAlchemyUploadRepository,
    store: S3ObjectStore,
    *,
    token: str,
    expires_at,
) -> tuple[c.UploadSession, str]:
    staging = store.prepare_upload(f"{token}.ttf", "incoming/uploads", content_key=token)
    upload_id = store.create_multipart_upload(staging.uri, content_type="font/ttf")
    _put_part(store, staging.uri, upload_id, 1, b"x" * PART_SIZE)
    upload = repository.create_upload(
        c.UploadSession(
            id=new_id("upl"),
            client_upload_id=f"client_{token}",
            owner_user_id="usr_admin",
            kind=c.UploadKind.font,
            filename=f"{token}.ttf",
            content_type="font/ttf",
            size_bytes=2 * PART_SIZE,
            upload_strategy=c.UploadStrategy.multipart,
            part_size_bytes=PART_SIZE,
            part_count=2,
            object_uri=staging.uri,
            staging_uri=staging.uri,
            expires_at=expires_at,
        ),
        multipart_upload_id=upload_id,
    )
    return upload, upload_id


def _assert_aborted(store: S3ObjectStore, uri: str, upload_id: str) -> None:
    with pytest.raises(Exception) as captured:
        store.list_parts(uri, upload_id=upload_id)
    response = getattr(captured.value, "response", {})
    assert str(response.get("Error", {}).get("Code")) in {
        "NoSuchUpload",
        "NoSuchKey",
        "404",
    }


def _empty_bucket(store: S3ObjectStore) -> None:
    client = store._client
    uploads = client.list_multipart_uploads(Bucket=store.bucket).get("Uploads", [])
    for upload in uploads:
        client.abort_multipart_upload(
            Bucket=store.bucket,
            Key=upload["Key"],
            UploadId=upload["UploadId"],
        )
    objects = client.list_objects_v2(Bucket=store.bucket).get("Contents", [])
    if objects:
        client.delete_objects(
            Bucket=store.bucket,
            Delete={"Objects": [{"Key": item["Key"]} for item in objects]},
        )
    client.delete_bucket(Bucket=store.bucket)


def test_real_multipart_refresh_resume_repeat_complete_cancel_and_expire(
    db_session_factory, tmp_path
) -> None:
    bucket = f"cutagent-upload-{uuid4().hex}"
    store = _store(tmp_path, bucket, "first-cache")
    repository = SqlAlchemyUploadRepository(db_session_factory)
    try:
        size_bytes = 23 * MIB
        prefix = minimal_ttf_bytes(family="MinIO resumable upload")
        content = prefix + bytes(size_bytes - len(prefix))
        digest = hashlib.sha256(content).hexdigest()
        token = uuid4().hex
        staging = store.prepare_upload("resume.ttf", "incoming/uploads", content_key=token)
        upload_id = store.create_multipart_upload(staging.uri, content_type="font/ttf")
        upload = repository.create_upload(
            c.UploadSession(
                id=new_id("upl"),
                client_upload_id=f"client_{token}",
                owner_user_id="usr_admin",
                kind=c.UploadKind.font,
                filename="resume.ttf",
                content_type="font/ttf",
                size_bytes=size_bytes,
                client_expected_sha256=digest,
                upload_strategy=c.UploadStrategy.multipart,
                part_size_bytes=PART_SIZE,
                part_count=3,
                object_uri=staging.uri,
                staging_uri=staging.uri,
                expires_at=c.utcnow() + timedelta(days=1),
            ),
            multipart_upload_id=upload_id,
        )

        # Two 8 MiB parts are 69.6% of this 23 MiB file. Simulate a browser close.
        _put_part(store, staging.uri, upload_id, 1, content[:PART_SIZE])
        _put_part(store, staging.uri, upload_id, 2, content[PART_SIZE : 2 * PART_SIZE])

        # A fresh store/cache represents the reopened page: ListParts, not local
        # progress, is authoritative and only part 3 is uploaded.
        resumed_store = _store(tmp_path, bucket, "reopened-cache")
        completed = resumed_store.list_parts(staging.uri, upload_id=upload_id)
        assert [part.part_number for part in completed] == [1, 2]
        _put_part(
            resumed_store,
            staging.uri,
            upload_id,
            3,
            content[2 * PART_SIZE :],
        )

        repository.mark_completing(
            upload.id,
            size_bytes=size_bytes,
            expected_sha256=digest,
            metadata={"title": "MinIO resumed font"},
        )
        reconciler = UploadReconciler(repository, resumed_store, UploadSettings())
        ready = reconciler.process(upload.id)
        assert ready.status == c.UploadStatus.ready
        assert ready.canonical_sha256 == digest
        first_resources = repository.ready_resources(upload.id)

        # The client may lose the 202/ready response and repeat completion.
        repeated = repository.mark_completing(
            upload.id,
            size_bytes=size_bytes,
            expected_sha256=digest,
            metadata={"title": "MinIO resumed font"},
        )
        second_resources = repository.ready_resources(upload.id)
        assert repeated.status == c.UploadStatus.ready
        assert first_resources[1].artifact_id == second_resources[1].artifact_id
        assert first_resources[2].id == second_resources[2].id

        cancel_upload, cancel_multipart_id = _incomplete_session(
            repository,
            resumed_store,
            token=uuid4().hex,
            expires_at=c.utcnow() + timedelta(days=1),
        )
        fake_app = SimpleNamespace(
            state=SimpleNamespace(
                object_store=resumed_store,
                sqlalchemy_upload_repository=repository,
                upload_reconciler=UploadReconciler(
                    repository,
                    resumed_store,
                    UploadSettings(),
                ),
            )
        )
        request = Request({"type": "http", "app": fake_app})
        admin = c.AuthUser(
            id="usr_admin",
            email="admin@local.cutagent",
            display_name="Admin",
            role=c.UserRole.admin,
        )
        cancelled = upload_service.cancel_upload(cancel_upload.id, request, admin)
        assert cancelled.status == c.UploadStatus.cancelled
        _assert_aborted(
            resumed_store,
            cancel_upload.staging_uri,
            cancel_multipart_id,
        )

        expired_upload, expired_multipart_id = _incomplete_session(
            repository,
            resumed_store,
            token=uuid4().hex,
            expires_at=c.utcnow() - timedelta(seconds=1),
        )
        assert reconciler.reconcile_once() >= 1
        expired = repository.get_upload(expired_upload.id)
        assert expired is not None and expired.status == c.UploadStatus.expired
        _assert_aborted(
            resumed_store,
            expired_upload.staging_uri,
            expired_multipart_id,
        )
    finally:
        _empty_bucket(store)
