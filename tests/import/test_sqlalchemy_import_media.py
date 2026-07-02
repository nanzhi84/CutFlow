import json
import sqlite3

from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from packages.core.contracts import CreateImportBatchRequest
from packages.core.storage.database import ArtifactRow, CaseRow, ImportBatchReportRow, MediaAssetRow
from packages.production import SqlAlchemyProductionRepository


sqlite3.register_adapter(list, json.dumps)


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(_type, _compiler, **_kw):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(_type, _compiler, **_kw):
    return "JSON"


def _repository_with_sqlite():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    for table in (CaseRow.__table__, ArtifactRow.__table__, MediaAssetRow.__table__, ImportBatchReportRow.__table__):
        table.create(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as session:
        session.add(
            CaseRow(
                id="case_demo",
                name="Demo case",
                owner_user_id="usr_admin",
                status="active",
                description="",
            )
        )
        session.commit()
    return SqlAlchemyProductionRepository(session_factory), session_factory


def test_sqlalchemy_media_import_with_uri_creates_uploaded_file_source_artifact():
    repository, session_factory = _repository_with_sqlite()
    row = {
        "external_id": "media_uri_ext",
        "case_id": "case_demo",
        "title": "Imported URI media",
        "kind": "video",
        "uri": "s3://cutagent-durable/imports/case_demo/broll.mp4",
        "mime": "video/mp4",
        "sha256": "abc123",
        "duration_sec": 12.5,
        "width": 1920,
        "height": 1080,
    }

    report = repository.create_import_batch(
        CreateImportBatchRequest(import_type="media", rows=[row]),
        request_id="req_sql_import_media_uri",
    )

    assert report is not None
    assert report.created_count == 1
    assert report.skipped_count == 0
    asset_id = report.results[0].internal_id
    with session_factory() as session:
        asset = session.get(MediaAssetRow, asset_id)
        assert asset is not None
        assert asset.source_artifact_id is not None
        artifact = session.get(ArtifactRow, asset.source_artifact_id)
        assert artifact is not None
        assert artifact.case_id == row["case_id"]
        assert artifact.kind == "uploaded.file"
        assert artifact.uri == row["uri"]
        assert artifact.sha256 == row["sha256"]
        assert artifact.payload_schema == "UploadedFileArtifact.v1"
        assert artifact.payload["filename"] == "broll.mp4"
        assert artifact.payload["content_type"] == row["mime"]
        assert artifact.payload["object_uri"] == row["uri"]
        assert artifact.payload["sha256"] == row["sha256"]
        assert artifact.payload["metadata"]["duration_sec"] == row["duration_sec"]
        assert artifact.payload["metadata"]["width"] == row["width"]
        assert artifact.payload["metadata"]["height"] == row["height"]
        assert artifact.media_info["mime_type"] == row["mime"]
        assert artifact.media_info["duration_sec"] == row["duration_sec"]


def test_sqlalchemy_media_import_with_uri_is_idempotent_by_sha256():
    repository, session_factory = _repository_with_sqlite()
    row = {
        "external_id": "media_uri_idempotent",
        "case_id": "case_demo",
        "title": "Imported URI media",
        "kind": "video",
        "uri": "s3://cutagent-durable/imports/case_demo/reused.mp4",
        "mime": "video/mp4",
        "sha256": "dedupe-sha",
    }

    first = repository.create_import_batch(
        CreateImportBatchRequest(import_type="media", rows=[row]),
        request_id="req_sql_import_media_first",
    )
    second = repository.create_import_batch(
        CreateImportBatchRequest(import_type="media", rows=[row]),
        request_id="req_sql_import_media_second",
    )

    assert first is not None
    assert second is not None
    assert first.created_count == 1
    assert first.skipped_count == 0
    assert second.created_count == 0
    assert second.skipped_count == 1
    assert second.results[0].status == "skipped"
    assert second.results[0].internal_id == first.results[0].internal_id
    with session_factory() as session:
        assets = list(
            session.scalars(
                select(MediaAssetRow).where(
                    MediaAssetRow.case_id == row["case_id"],
                    MediaAssetRow.kind == row["kind"],
                )
            )
        )
        artifacts = list(
            session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.kind == "uploaded.file",
                    ArtifactRow.sha256 == row["sha256"],
                    ArtifactRow.uri == row["uri"],
                )
            )
        )
    assert len(assets) == 1
    assert len(artifacts) == 1


def test_sqlalchemy_media_import_with_uri_is_idempotent_by_uri_when_sha256_missing():
    repository, session_factory = _repository_with_sqlite()
    row = {
        "external_id": "media_uri_idempotent_no_sha",
        "case_id": "case_demo",
        "title": "Imported URI media without sha",
        "kind": "video",
        "uri": "s3://cutagent-durable/imports/case_demo/no-sha.mp4",
        "mime": "video/mp4",
    }

    first = repository.create_import_batch(
        CreateImportBatchRequest(import_type="media", rows=[row]),
        request_id="req_sql_import_media_uri_first",
    )
    second = repository.create_import_batch(
        CreateImportBatchRequest(import_type="media", rows=[row]),
        request_id="req_sql_import_media_uri_second",
    )

    assert first is not None
    assert second is not None
    assert first.created_count == 1
    assert second.created_count == 0
    assert second.skipped_count == 1
    assert second.results[0].internal_id == first.results[0].internal_id
    with session_factory() as session:
        assets = list(
            session.scalars(
                select(MediaAssetRow).where(
                    MediaAssetRow.case_id == row["case_id"],
                    MediaAssetRow.kind == row["kind"],
                )
            )
        )
        artifacts = list(
            session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.kind == "uploaded.file",
                    ArtifactRow.sha256.is_(None),
                    ArtifactRow.uri == row["uri"],
                )
            )
        )
    assert len(assets) == 1
    assert len(artifacts) == 1
