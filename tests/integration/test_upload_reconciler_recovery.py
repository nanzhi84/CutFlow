from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from math import ceil
from threading import Lock

import pytest
from sqlalchemy import func, select

from packages.core import contracts as c
from packages.core.config.settings import UploadSettings
from packages.core.storage.database import ArtifactRow, MediaAssetRow
from packages.core.storage.object_store import LocalObjectStore, parse_object_uri
from packages.core.storage.repository import new_id
from packages.core.storage.sqlalchemy_uploads import SqlAlchemyUploadRepository
from packages.media.upload_reconciler import UploadReconciler
from tests.api._upload_helpers import minimal_ttf_bytes


def _repository(db_session_factory) -> SqlAlchemyUploadRepository:
    return SqlAlchemyUploadRepository(db_session_factory)


def _create_upload(
    repository: SqlAlchemyUploadRepository,
    store: LocalObjectStore,
    content: bytes,
    *,
    multipart: bool,
    expires_at=None,
) -> c.UploadSession:
    upload_id = new_id("upl")
    staging = store.prepare_upload(f"{upload_id}.ttf", "incoming/uploads", content_key=upload_id)
    upload_id_remote = None
    part_size = None
    part_count = 1
    strategy = c.UploadStrategy.single
    if multipart:
        strategy = c.UploadStrategy.multipart
        part_size = ceil(len(content) / 2)
        part_count = ceil(len(content) / part_size)
        upload_id_remote = store.create_multipart_upload(staging.uri, content_type="font/ttf")
        for part_number in range(1, part_count + 1):
            start = (part_number - 1) * part_size
            signed = store.sign_upload_part(
                staging.uri,
                upload_id=upload_id_remote,
                part_number=part_number,
                expires_in=timedelta(minutes=5),
            )
            store.put_bytes(parse_object_uri(signed.url), content[start : start + part_size])
    else:
        store.put_bytes(staging, content)
    upload = repository.create_upload(
        c.UploadSession(
            id=upload_id,
            client_upload_id=f"client_{upload_id}",
            owner_user_id="usr_admin",
            kind=c.UploadKind.font,
            filename=f"{upload_id}.ttf",
            content_type="font/ttf",
            size_bytes=len(content),
            client_expected_sha256=None,
            upload_strategy=strategy,
            part_size_bytes=part_size,
            part_count=part_count,
            object_uri=staging.uri,
            staging_uri=staging.uri,
            expires_at=expires_at or c.utcnow() + timedelta(days=1),
        ),
        multipart_upload_id=upload_id_remote,
    )
    return repository.mark_completing(
        upload.id,
        size_bytes=len(content),
        expected_sha256=None,
        metadata={"title": "Crash-safe font"},
    )


def _assert_single_registration(db_session_factory, upload_id: str) -> None:
    with db_session_factory() as session:
        artifacts = session.scalar(
            select(func.count())
            .select_from(ArtifactRow)
            .where(ArtifactRow.source_upload_session_id == upload_id)
        )
        assets = session.scalar(
            select(func.count())
            .select_from(MediaAssetRow)
            .join(ArtifactRow, MediaAssetRow.source_artifact_id == ArtifactRow.id)
            .where(ArtifactRow.source_upload_session_id == upload_id)
        )
    assert artifacts == 1
    assert assets == 1


def _advance_to_verified(
    repository: SqlAlchemyUploadRepository,
    reconciler: UploadReconciler,
    upload_id: str,
) -> c.UploadSession:
    upload = repository.get_upload(upload_id)
    assert upload is not None
    reconciler._complete_object(upload)
    upload = repository.get_upload(upload_id)
    assert upload is not None and upload.status == c.UploadStatus.object_completed
    reconciler._verify_and_promote(upload)
    verified = repository.get_upload(upload_id)
    assert verified is not None and verified.status == c.UploadStatus.verified
    return verified


def test_recovers_when_process_exits_after_complete_multipart(
    db_session_factory, tmp_path, monkeypatch
) -> None:
    repository = _repository(db_session_factory)
    store = LocalObjectStore(tmp_path / "objects")
    upload = _create_upload(
        repository,
        store,
        minimal_ttf_bytes(family="Crash after complete"),
        multipart=True,
    )
    reconciler = UploadReconciler(repository, store, UploadSettings())
    real_patch = repository.patch_upload
    crashed = False

    def crash_before_object_completed(upload_id: str, updates: dict):
        nonlocal crashed
        if updates.get("status") == c.UploadStatus.object_completed and not crashed:
            crashed = True
            raise RuntimeError("injected exit after CompleteMultipartUpload")
        return real_patch(upload_id, updates)

    monkeypatch.setattr(repository, "patch_upload", crash_before_object_completed)
    interrupted = reconciler.process(upload.id)
    assert interrupted.status == c.UploadStatus.completing
    assert interrupted.retry_count == 1
    assert interrupted.staging_uri is not None
    assert store.exists(parse_object_uri(interrupted.staging_uri))

    monkeypatch.setattr(repository, "patch_upload", real_patch)
    recovered = reconciler.process(upload.id)
    assert recovered.status == c.UploadStatus.ready
    _assert_single_registration(db_session_factory, upload.id)


def test_recovers_when_process_exits_after_verified_copy(
    db_session_factory, tmp_path, monkeypatch
) -> None:
    repository = _repository(db_session_factory)
    store = LocalObjectStore(tmp_path / "objects")
    upload = _create_upload(
        repository,
        store,
        minimal_ttf_bytes(family="Crash after copy"),
        multipart=False,
    )
    reconciler = UploadReconciler(repository, store, UploadSettings())
    real_patch = repository.patch_upload
    crashed = False

    def crash_before_verified(upload_id: str, updates: dict):
        nonlocal crashed
        if updates.get("status") == c.UploadStatus.verified and not crashed:
            crashed = True
            raise RuntimeError("injected exit after final object copy")
        return real_patch(upload_id, updates)

    monkeypatch.setattr(repository, "patch_upload", crash_before_verified)
    interrupted = reconciler.process(upload.id)
    assert interrupted.status == c.UploadStatus.object_completed
    assert interrupted.retry_count == 1

    monkeypatch.setattr(repository, "patch_upload", real_patch)
    recovered = reconciler.process(upload.id)
    assert recovered.status == c.UploadStatus.ready
    _assert_single_registration(db_session_factory, upload.id)


def test_cancel_wins_race_after_final_copy_and_cleans_deterministic_objects(
    db_session_factory, tmp_path, monkeypatch
) -> None:
    repository = _repository(db_session_factory)
    store = LocalObjectStore(tmp_path / "objects")
    upload = _create_upload(
        repository,
        store,
        minimal_ttf_bytes(family="Cancel during copy"),
        multipart=False,
    )
    reconciler = UploadReconciler(repository, store, UploadSettings())
    reconciler._complete_object(upload)
    object_completed = repository.get_upload(upload.id)
    assert object_completed is not None
    assert object_completed.status == c.UploadStatus.object_completed
    assert object_completed.staging_uri is not None
    final_uri = reconciler._final_uri_for(object_completed.staging_uri, object_completed.kind)
    real_copy = store.copy

    def copy_then_cancel(src_uri: str, dst_uri: str) -> None:
        real_copy(src_uri, dst_uri)
        repository.patch_upload(upload.id, {"status": c.UploadStatus.cancelled})

    monkeypatch.setattr(store, "copy", copy_then_cancel)
    cancelled = reconciler.process(upload.id)

    assert cancelled.status == c.UploadStatus.cancelled
    assert not store.exists(parse_object_uri(object_completed.staging_uri))
    assert not store.exists(parse_object_uri(final_uri))
    assert repository.artifact_for_upload(upload.id) is None


def test_commit_succeeded_but_response_was_lost_is_idempotent(
    db_session_factory, tmp_path, monkeypatch
) -> None:
    repository = _repository(db_session_factory)
    store = LocalObjectStore(tmp_path / "objects")
    upload = _create_upload(
        repository,
        store,
        minimal_ttf_bytes(family="Lost registration response"),
        multipart=False,
    )
    reconciler = UploadReconciler(repository, store, UploadSettings())
    real_finalize = repository.finalize_ready
    crashed = False

    def commit_then_exit(upload_id: str):
        nonlocal crashed
        result = real_finalize(upload_id)
        if not crashed:
            crashed = True
            raise RuntimeError("injected exit after registration commit")
        return result

    monkeypatch.setattr(repository, "finalize_ready", commit_then_exit)
    committed = reconciler.process(upload.id)
    assert committed.status == c.UploadStatus.ready
    assert committed.retry_count == 1

    monkeypatch.setattr(repository, "finalize_ready", real_finalize)
    recovered = reconciler.process(upload.id)
    repeated = repository.mark_completing(
        upload.id,
        size_bytes=upload.size_bytes,
        expected_sha256=None,
        metadata={"title": "Crash-safe font"},
    )
    assert recovered.status == repeated.status == c.UploadStatus.ready
    _assert_single_registration(db_session_factory, upload.id)


def test_registration_transaction_rolls_back_artifact_and_asset_together(
    db_session_factory, tmp_path, monkeypatch
) -> None:
    repository = _repository(db_session_factory)
    store = LocalObjectStore(tmp_path / "objects")
    upload = _create_upload(
        repository,
        store,
        minimal_ttf_bytes(family="Atomic registration"),
        multipart=False,
    )
    reconciler = UploadReconciler(repository, store, UploadSettings())
    _advance_to_verified(repository, reconciler, upload.id)
    real_create_asset = repository._get_or_create_media_asset

    def fail_after_artifact_flush(*_args, **_kwargs):
        raise RuntimeError("injected transaction rollback")

    monkeypatch.setattr(repository, "_get_or_create_media_asset", fail_after_artifact_flush)
    with pytest.raises(RuntimeError, match="transaction rollback"):
        repository.finalize_ready(upload.id)

    assert repository.get_upload(upload.id).status == c.UploadStatus.verified
    assert repository.artifact_for_upload(upload.id) is None

    monkeypatch.setattr(repository, "_get_or_create_media_asset", real_create_asset)
    ready, _, asset, _ = repository.finalize_ready(upload.id)
    assert ready.status == c.UploadStatus.ready
    assert asset is not None
    _assert_single_registration(db_session_factory, upload.id)


def test_concurrent_registration_returns_one_artifact_and_business_object(
    db_session_factory, tmp_path
) -> None:
    repository = _repository(db_session_factory)
    store = LocalObjectStore(tmp_path / "objects")
    upload = _create_upload(
        repository,
        store,
        minimal_ttf_bytes(family="Concurrent registration"),
        multipart=False,
    )
    reconciler = UploadReconciler(repository, store, UploadSettings())
    _advance_to_verified(repository, reconciler, upload.id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(repository.finalize_ready, [upload.id, upload.id]))

    assert len({result[1].artifact_id for result in results}) == 1
    assert len({result[2].id for result in results if result[2] is not None}) == 1
    _assert_single_registration(db_session_factory, upload.id)


def test_concurrent_prepare_and_multipart_creation_have_one_winner(
    db_session_factory,
) -> None:
    repository = _repository(db_session_factory)
    stable_client_id = f"client_{new_id('stable')}"

    def candidate() -> c.UploadSession:
        upload_id = new_id("upl")
        uri = f"local://cutagent-local/incoming/uploads/{upload_id}/same.ttf"
        return c.UploadSession(
            id=upload_id,
            client_upload_id=stable_client_id,
            owner_user_id="usr_admin",
            kind=c.UploadKind.font,
            filename="same.ttf",
            content_type="font/ttf",
            size_bytes=16 * 1024 * 1024,
            client_expected_sha256="a" * 64,
            upload_strategy=c.UploadStrategy.multipart,
            part_size_bytes=8 * 1024 * 1024,
            part_count=2,
            object_uri=uri,
            staging_uri=uri,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(executor.map(repository.create_upload, [candidate(), candidate()]))

    assert len({upload.id for upload in created}) == 1
    winner_id = created[0].id
    create_calls = 0
    call_lock = Lock()

    def create_remote_upload() -> str:
        nonlocal create_calls
        with call_lock:
            create_calls += 1
        return "mpu_one_winner"

    with ThreadPoolExecutor(max_workers=2) as executor:
        remote_ids = list(
            executor.map(
                lambda _index: repository.ensure_multipart_upload_id(
                    winner_id, create_remote_upload
                ),
                range(2),
            )
        )

    assert remote_ids == ["mpu_one_winner", "mpu_one_winner"]
    assert create_calls == 1


def test_expired_incomplete_multipart_is_aborted(db_session_factory, tmp_path) -> None:
    repository = _repository(db_session_factory)
    store = LocalObjectStore(tmp_path / "objects")
    upload = _create_upload(
        repository,
        store,
        minimal_ttf_bytes(family="Expired upload"),
        multipart=True,
        expires_at=c.utcnow() - timedelta(seconds=1),
    )
    staging_uri = upload.staging_uri
    multipart_upload_id = repository.multipart_upload_id(upload.id)
    assert staging_uri is not None and multipart_upload_id is not None

    claimed = repository.claim_reconcilable(owner="active-worker", limit=1, lease_seconds=300)
    assert [item.id for item in claimed] == [upload.id]
    assert repository.expire_stale_uploads() == []
    repository.clear_lease(upload.id)

    reconciler = UploadReconciler(repository, store, UploadSettings())
    assert reconciler.reconcile_once() >= 1
    expired = repository.get_upload(upload.id)
    assert expired is not None and expired.status == c.UploadStatus.expired
    with pytest.raises(FileNotFoundError):
        store.list_parts(staging_uri, upload_id=multipart_upload_id)
